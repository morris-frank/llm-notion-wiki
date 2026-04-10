from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any
from urllib import error
from uuid import uuid4

from .models import (
    EntityRecord,
    JobRecord,
    PolicyRecord,
    PromotionRecord,
    QuestionRecord,
    ScopeContext,
    SourceRecord,
    WebhookResolvePromotion,
    WebhookResolveQuestion,
    WebhookResolveResult,
    WebhookResolveSource,
    WikiPageMetadata,
)

from .notion import (
    NotionAPIError,
    NotionClient,
    checkbox_property,
    date_property,
    multi_select_names,
    number_property,
    plain_text,
    relation_property,
    rich_text_property,
    select_property,
    title_property,
    url_property,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _prop(page: dict[str, Any], name: str) -> dict[str, Any] | None:
    return page.get("properties", {}).get(name)


def _title(page: dict[str, Any], name: str) -> str:
    return plain_text((_prop(page, name) or {}).get("title"))


def _rich_text(page: dict[str, Any], name: str) -> str:
    return plain_text((_prop(page, name) or {}).get("rich_text"))


def _select(page: dict[str, Any], name: str) -> str | None:
    return ((_prop(page, name) or {}).get("select") or {}).get("name")


def _checkbox(page: dict[str, Any], name: str) -> bool:
    return bool((_prop(page, name) or {}).get("checkbox"))


def _url(page: dict[str, Any], name: str) -> str | None:
    return (_prop(page, name) or {}).get("url")


def _number(page: dict[str, Any], name: str) -> int | None:
    value = (_prop(page, name) or {}).get("number")
    return int(value) if value is not None else None


def _date(page: dict[str, Any], name: str) -> str | None:
    date_data = (_prop(page, name) or {}).get("date") or {}
    return date_data.get("start")


def _relation_ids(page: dict[str, Any], name: str) -> list[str]:
    return [entry["id"] for entry in (_prop(page, name) or {}).get("relation", [])]


def _multi_select(page: dict[str, Any], name: str) -> list[str]:
    return multi_select_names((_prop(page, name) or {}).get("multi_select"))


def _scope_context(page: dict[str, Any]) -> ScopeContext:
    scope = _select(page, "Scope") or "shared"
    owner = _rich_text(page, "Owner") or None
    return ScopeContext(scope, owner)


def _policy_score(row: dict[str, Any], scope_context: ScopeContext) -> tuple[int, int, str] | None:
    row_scope = _select(row, "Policy Target Scope") or "all"
    row_owner = _rich_text(row, "Policy Owner") or None
    priority = _number(row, "Policy Priority") or 0
    if scope_context.scope == "shared":
        if row_owner is not None:
            return None
        if row_scope == "shared":
            return (0, -priority, row["id"])
        if row_scope == "all":
            return (1, -priority, row["id"])
        return None
    if row_scope == "private" and row_owner == scope_context.owner:
        return (0, -priority, row["id"])
    if row_scope == "private" and row_owner is None:
        return (1, -priority, row["id"])
    if row_scope == "all" and row_owner == scope_context.owner:
        return (2, -priority, row["id"])
    if row_scope == "all" and row_owner is None:
        return (3, -priority, row["id"])
    return None


@dataclass
class NotionRepository:
    client: NotionClient
    sources_data_source_id: str
    wiki_data_source_id: str
    jobs_data_source_id: str
    policies_data_source_id: str
    entities_data_source_id: str | None = None
    questions_data_source_id: str | None = None
    promotions_data_source_id: str | None = None

    def _source_from_page(self, page: dict[str, Any]) -> SourceRecord:
        scope_context = _scope_context(page)
        return SourceRecord(
            page_id=page["id"],
            source_id=_rich_text(page, "Source ID") or page["id"],
            source_type=_select(page, "Source Type") or "web_page",
            title=_title(page, "Source Title") or "Untitled Source",
            canonical_url=_url(page, "Canonical URL"),
            trust_level=_select(page, "Trust Level"),
            status=_select(page, "Source Status"),
            scope=scope_context.scope,
            owner=scope_context.owner,
            target_page_id=_rich_text(page, "Target Notion Page ID") or None,
            content_version=_number(page, "Content Version"),
            checksum=_rich_text(page, "Source Checksum") or None,
            trigger_regeneration=_checkbox(page, "Trigger Regeneration"),
            raw_text_pointer=_url(page, "Raw Text Pointer"),
            markdown_pointer=_url(page, "Normalised Markdown Pointer"),
            source_summary_pointer=_url(page, "Source Summary Pointer"),
            last_edited_time=page.get("last_edited_time"),
            properties=page["properties"],
        )

    def _job_from_page(self, page: dict[str, Any]) -> JobRecord:
        scope_context = _scope_context(page)
        return JobRecord(
            page_id=page["id"],
            job_id=_rich_text(page, "Job ID") or page["id"],
            job_type=_select(page, "Job Type") or "",
            status=_select(page, "Job Status") or "queued",
            queue_timestamp=_date(page, "Queue Timestamp"),
            scope=scope_context.scope,
            owner=scope_context.owner,
            target_source_page_id=(_relation_ids(page, "Target Source") or [None])[0],
            target_wiki_page_id=(_relation_ids(page, "Target Wiki Page") or [None])[0],
            target_question_page_id=(_relation_ids(page, "Target Question") or [None])[0],
            target_promotion_page_id=(_relation_ids(page, "Target Promotion") or [None])[0],
            idempotency_key=_rich_text(page, "Idempotency Key") or None,
            policy_page_id=(_relation_ids(page, "Policy Version Ref") or [None])[0],
            attempt_count=_number(page, "Attempt Count"),
            properties=page["properties"],
        )

    def _policy_from_page(self, page: dict[str, Any], *, content_markdown: str) -> PolicyRecord:
        return PolicyRecord(
            page_id=page["id"],
            name=_title(page, "Policy Name") or "Unnamed Policy",
            version=_rich_text(page, "Policy Version") or None,
            target_scope=_select(page, "Policy Target Scope") or "all",
            owner=_rich_text(page, "Policy Owner") or None,
            priority=_number(page, "Policy Priority") or 0,
            active=_checkbox(page, "Active"),
            allowed_page_types=_multi_select(page, "Allowed Page Types"),
            question_mode=_select(page, "Question Mode") or "mixed",
            entity_extraction=_select(page, "Entity Extraction") or "minimal",
            promotion_required_for_shared=_checkbox(page, "Promotion Required For Shared"),
            minimum_review_state_for_shared=_select(page, "Minimum Review State For Shared"),
            requires_human_review=_checkbox(page, "Requires Human Review"),
            auto_publish_allowed=_checkbox(page, "Auto Publish Allowed"),
            max_source_count=_number(page, "Max Source Count"),
            prompt_bundle_pointer=_url(page, "Prompt Bundle Pointer"),
            citation_policy_pointer=_url(page, "Citation Policy Pointer"),
            page_template_pointer=_url(page, "Page Template Pointer"),
            content_markdown=content_markdown,
            properties=page["properties"],
        )

    def _question_from_page(self, page: dict[str, Any]) -> QuestionRecord:
        scope_context = _scope_context(page)
        return QuestionRecord(
            page_id=page["id"],
            question_id=_rich_text(page, "Question ID") or page["id"],
            question=_title(page, "Question") or "Untitled Question",
            status=_select(page, "Question Status") or "queued",
            scope=scope_context.scope,
            owner=scope_context.owner,
            latest_job_page_id=(_relation_ids(page, "Latest Job") or [None])[0],
            target_wiki_page_id=(_relation_ids(page, "Target Wiki Page") or [None])[0],
            answer_page_slug=_rich_text(page, "Answer Page Slug") or None,
            resolution_type=_select(page, "Resolution Type") or None,
            properties=page["properties"],
        )

    def _promotion_from_page(self, page: dict[str, Any]) -> PromotionRecord:
        scope_context = _scope_context(page)
        return PromotionRecord(
            page_id=page["id"],
            promotion_id=_rich_text(page, "Promotion ID") or page["id"],
            scope=scope_context.scope,
            owner=scope_context.owner,
            status=_select(page, "Status") or "pending",
            decision=_rich_text(page, "Decision") or None,
            submitted_by=_rich_text(page, "Submitted By") or None,
            reviewed_by=_rich_text(page, "Reviewed By") or None,
            source_private_page_id=(_relation_ids(page, "Source Private Page") or [None])[0],
            target_shared_page_ids=_relation_ids(page, "Target Shared Pages"),
            latest_job_page_id=(_relation_ids(page, "Latest Job") or [None])[0],
            properties=page["properties"],
        )

    def _entity_from_page(self, page: dict[str, Any]) -> EntityRecord:
        return EntityRecord(
            page_id=page["id"],
            canonical_entity_id=_rich_text(page, "Canonical Entity ID") or page["id"],
            name=_title(page, "Entity Name") or "Unnamed Entity",
            entity_type=_select(page, "Entity Type") or "concept",
            properties=page["properties"],
        )

    def get_source(self, source_page_id: str) -> SourceRecord:
        return self._source_from_page(self.client.retrieve_page(source_page_id))

    def resolve_webhook_page(self, page_id: str) -> WebhookResolveResult | None:
        try:
            page = self.client.retrieve_page(page_id)
        except (NotionAPIError, error.HTTPError):
            return None
        try:
            properties = page.get("properties", {})
            if "Source Title" in properties:
                return WebhookResolveSource(source=self._source_from_page(page))
            if "Question" in properties and self.questions_data_source_id:
                return WebhookResolveQuestion(question=self._question_from_page(page))
            if "Promotion ID" in properties and self.promotions_data_source_id:
                return WebhookResolvePromotion(promotion=self._promotion_from_page(page))
            source = self._source_from_page(page)
            if source.properties.get("Source Title") is None:
                return None
            return WebhookResolveSource(source=source)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def get_question(self, question_page_id: str) -> QuestionRecord:
        if not self.questions_data_source_id:
            raise ValueError("Questions data source is not configured")
        return self._question_from_page(self.client.retrieve_page(question_page_id))

    def get_promotion(self, promotion_page_id: str) -> PromotionRecord:
        if not self.promotions_data_source_id:
            raise ValueError("Promotions data source is not configured")
        return self._promotion_from_page(self.client.retrieve_page(promotion_page_id))

    def get_wiki_page(self, wiki_page_id: str) -> WikiPageMetadata:
        page = self.client.retrieve_page(wiki_page_id)
        scope_context = _scope_context(page)
        return WikiPageMetadata(
            path=_rich_text(page, "Canonical Markdown Path"),
            title=_title(page, "Wiki Title"),
            slug=_rich_text(page, "Wiki Slug"),
            page_type=_select(page, "Wiki Type") or "concept",
            status=_select(page, "Wiki Status") or "draft",
            confidence=_select(page, "Confidence Level") or "medium",
            review_required=_checkbox(page, "Needs Human Review"),
            source_ids=[],
            source_scope=[],
            entity_keys=[],
            scope=scope_context.scope,
            owner=scope_context.owner,
            review_state=_select(page, "Review State") or ("unreviewed" if scope_context.scope == "shared" else "n_a"),
            promotion_origin=_rich_text(page, "Promotion Origin") or None,
            summary=_rich_text(page, "Summary"),
        )

    def query_jobs(self, *, status: str | None = None, page_size: int = 20) -> list[JobRecord]:
        filter_obj = None
        if status:
            filter_obj = {"property": "Job Status", "select": {"equals": status}}
        result = self.client.query_data_source(
            self.jobs_data_source_id,
            filter_obj=filter_obj,
            sorts=[{"property": "Queue Timestamp", "direction": "ascending"}],
            page_size=page_size,
        )
        return [self._job_from_page(page) for page in result.get("results", [])]

    def query_queued_jobs(self) -> list[JobRecord]:
        result = self.client.query_data_source(
            self.jobs_data_source_id,
            filter_obj={
                "and": [
                    {"property": "Job Status", "select": {"equals": "queued"}},
                    {"property": "Locked", "checkbox": {"equals": False}},
                ]
            },
            sorts=[{"property": "Queue Timestamp", "direction": "ascending"}],
            page_size=20,
        )
        return [self._job_from_page(page) for page in result.get("results", [])]

    def claim_job(self, job: JobRecord, worker_name: str) -> str | None:
        started_at = now_iso()
        claim_worker_name = f"{worker_name}#{uuid4().hex[:8]}"
        props = {
            "Job Status": select_property("running"),
            "Job Phase": select_property("running"),
            "Started At": date_property(started_at),
            "Locked": checkbox_property(True),
            "Worker Name": rich_text_property(claim_worker_name),
            "Scope": select_property(job.scope),
            "Owner": rich_text_property(job.owner or ""),
        }
        self.client.update_page(job.page_id, props)
        page = self.client.retrieve_page(job.page_id)
        if (
            _select(page, "Job Status") != "running"
            or not _checkbox(page, "Locked")
            or _rich_text(page, "Worker Name") != claim_worker_name
            or _date(page, "Started At") != started_at
        ):
            return None
        job.properties.setdefault("Started At", {"date": {"start": started_at}})
        return started_at

    def update_job_phase(self, page_id: str, phase: str) -> None:
        self.client.update_page(page_id, {"Job Phase": select_property(phase)})

    def mark_job_failed(self, page_id: str, error_class: str, message: str, *, output_pointer: str | None = None) -> None:
        props = {
            "Job Status": select_property("failed"),
            "Job Phase": select_property(None),
            "Finished At": date_property(now_iso()),
            "Error Class": select_property(error_class),
            "Error Message": rich_text_property(message[:1800]),
            "Locked": checkbox_property(False),
        }
        if output_pointer:
            props["Output Pointer"] = url_property(output_pointer)
        self.client.update_page(page_id, props)

    def mark_job_succeeded(
        self,
        page_id: str,
        *,
        started_at: str | None,
        output_pointer: str | None,
        diff_pointer: str | None,
    ) -> None:
        finished_at = now_iso()
        duration_ms = None
        if started_at:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            duration_ms = int((finish - start).total_seconds() * 1000)
        props = {
            "Job Status": select_property("succeeded"),
            "Job Phase": select_property("syncing_state"),
            "Finished At": date_property(finished_at),
            "Locked": checkbox_property(False),
            "Error Class": select_property(None),
            "Error Message": rich_text_property(""),
        }
        if duration_ms is not None:
            props["Duration Ms"] = number_property(duration_ms)
        if output_pointer:
            props["Output Pointer"] = url_property(output_pointer)
        if diff_pointer:
            props["Diff Pointer"] = url_property(diff_pointer)
        self.client.update_page(page_id, props)

    def requeue_job(self, job_page_id: str) -> JobRecord:
        page = self.client.retrieve_page(job_page_id)
        attempt_count = _number(page, "Attempt Count") or 0
        self.client.update_page(
            job_page_id,
            {
                "Job Status": select_property("queued"),
                "Job Phase": select_property(None),
                "Locked": checkbox_property(False),
                "Error Class": select_property(None),
                "Error Message": rich_text_property(""),
                "Retry After Seconds": number_property(0),
                "Attempt Count": number_property(attempt_count + 1),
            },
        )
        return self._job_from_page(self.client.retrieve_page(job_page_id))

    def update_source_for_ingest(
        self,
        source: SourceRecord,
        *,
        checksum: str,
        raw_text_pointer: str,
        markdown_pointer: str,
    ) -> None:
        now = now_iso()
        self.client.update_page(
            source.page_id,
            {
                "Source Status": select_property("parsed"),
                "Scope": select_property(source.scope),
                "Owner": rich_text_property(source.owner or ""),
                "Source Checksum": rich_text_property(checksum),
                "Raw Text Pointer": url_property(raw_text_pointer),
                "Normalised Markdown Pointer": url_property(markdown_pointer),
                "Last Parsed At": date_property(now),
                "Last Seen At": date_property(now),
            },
        )

    def mark_source_fetching(self, source: SourceRecord) -> None:
        self.client.update_page(
            source.page_id,
            {
                "Source Status": select_property("fetching"),
                "Scope": select_property(source.scope),
                "Owner": rich_text_property(source.owner or ""),
            },
        )

    def mark_source_failed(self, source: SourceRecord, message: str) -> None:
        self.client.update_page(
            source.page_id,
            {
                "Source Status": select_property("failed"),
                "Scope": select_property(source.scope),
                "Owner": rich_text_property(source.owner or ""),
                "Parse Error": rich_text_property(message[:1800]),
                "Last Error At": date_property(now_iso()),
            },
        )

    def update_source_after_wiki(self, source: SourceRecord, *, source_summary_pointer: str, related_entity_page_ids: list[str] | None = None) -> None:
        props = {
            "Source Status": select_property("processed"),
            "Scope": select_property(source.scope),
            "Owner": rich_text_property(source.owner or ""),
            "Source Summary Pointer": url_property(source_summary_pointer),
            "Last Processed At": date_property(now_iso()),
            "Trigger Regeneration": checkbox_property(False),
        }
        if self.entities_data_source_id and related_entity_page_ids is not None:
            props["Related Entities"] = relation_property(related_entity_page_ids)
        self.client.update_page(source.page_id, props)

    def find_existing_job_by_idempotency_key(self, key: str) -> JobRecord | None:
        result = self.client.query_data_source(
            self.jobs_data_source_id,
            filter_obj={"property": "Idempotency Key", "rich_text": {"equals": key}},
            page_size=1,
        )
        rows = result.get("results", [])
        return self._job_from_page(rows[0]) if rows else None

    def active_policy_page_id(self, scope_context: ScopeContext | None = None) -> str | None:
        rows = self.client.query_data_source(
            self.policies_data_source_id,
            filter_obj={"property": "Active", "checkbox": {"equals": True}},
            page_size=50,
        ).get("results", [])
        if scope_context is None:
            return rows[0]["id"] if rows else None
        best_score: tuple[int, int, str] | None = None
        best_id: str | None = None
        for row in rows:
            score = _policy_score(row, scope_context)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_id = row["id"]
        return best_id

    def load_effective_policy(self, scope_context: ScopeContext) -> PolicyRecord | None:
        policy_page_id = self.active_policy_page_id(scope_context)
        if not policy_page_id:
            return None
        page = self.client.retrieve_page(policy_page_id)
        content_markdown = self.client.page_markdown(policy_page_id, title=_title(page, "Policy Name"))
        return self._policy_from_page(page, content_markdown=content_markdown)

    def create_job(
        self,
        *,
        job_type: str,
        title: str,
        idempotency_key: str,
        scope_context: ScopeContext,
        target_source_page_id: str | None = None,
        target_wiki_page_id: str | None = None,
        target_question_page_id: str | None = None,
        target_promotion_page_id: str | None = None,
        policy_page_id: str | None = None,
        trigger_type: str = "manual",
    ) -> JobRecord:
        existing = self.find_existing_job_by_idempotency_key(idempotency_key)
        if existing:
            return existing
        job_id = f"job_{hashlib.sha1(idempotency_key.encode('utf-8')).hexdigest()[:12]}"
        properties = {
            "Job Title": title_property(title),
            "Job ID": rich_text_property(job_id),
            "Job Type": select_property(job_type),
            "Job Status": select_property("queued"),
            "Queue Timestamp": date_property(now_iso()),
            "Scope": select_property(scope_context.scope),
            "Owner": rich_text_property(scope_context.owner or ""),
            "Trigger Type": select_property(trigger_type),
            "Priority": select_property("normal"),
            "Attempt Count": number_property(0),
            "Max Attempts": number_property(8),
            "Idempotency Key": rich_text_property(idempotency_key),
            "Locked": checkbox_property(False),
        }
        if target_source_page_id:
            properties["Target Source"] = relation_property([target_source_page_id])
        if target_wiki_page_id:
            properties["Target Wiki Page"] = relation_property([target_wiki_page_id])
        if target_question_page_id:
            properties["Target Question"] = relation_property([target_question_page_id])
        if target_promotion_page_id:
            properties["Target Promotion"] = relation_property([target_promotion_page_id])
        if policy_page_id:
            properties["Policy Version Ref"] = relation_property([policy_page_id])
        page = self.client.create_page(self.jobs_data_source_id, properties)
        return JobRecord(
            page_id=page["id"],
            job_id=job_id,
            job_type=job_type,
            status="queued",
            queue_timestamp=None,
            scope=scope_context.scope,
            owner=scope_context.owner,
            target_source_page_id=target_source_page_id,
            target_wiki_page_id=target_wiki_page_id,
            target_question_page_id=target_question_page_id,
            target_promotion_page_id=target_promotion_page_id,
            idempotency_key=idempotency_key,
            policy_page_id=policy_page_id,
            properties=page["properties"],
        )

    def find_wiki_page_by_slug(self, slug: str, *, scope_context: ScopeContext) -> dict[str, Any] | None:
        clauses: list[dict[str, Any]] = [
            {"property": "Wiki Slug", "rich_text": {"equals": slug}},
            {"property": "Scope", "select": {"equals": scope_context.scope}},
        ]
        if scope_context.scope == "private":
            clauses.append({"property": "Owner", "rich_text": {"equals": scope_context.owner}})
        result = self.client.query_data_source(self.wiki_data_source_id, filter_obj={"and": clauses}, page_size=1)
        rows = result.get("results", [])
        return rows[0] if rows else None

    def resolve_backing_source_page_ids(self, source_ids: list[str], *, page_scope_context: ScopeContext) -> list[str]:
        if not source_ids:
            return []
        result = self.client.query_data_source(
            self.sources_data_source_id,
            filter_obj={"or": [{"property": "Source ID", "rich_text": {"equals": source_id}} for source_id in source_ids]},
            page_size=max(25, len(source_ids) * 4),
        )
        by_source_id: dict[str, list[SourceRecord]] = {}
        for page in result.get("results", []):
            record = self._source_from_page(page)
            by_source_id.setdefault(record.source_id, []).append(record)
        resolved_page_ids: list[str] = []
        for source_id in source_ids:
            candidates = by_source_id.get(source_id, [])
            allowed: list[SourceRecord] = []
            for candidate in candidates:
                if page_scope_context.scope == "shared":
                    if candidate.scope == "shared":
                        allowed.append(candidate)
                    continue
                if candidate.scope == "shared":
                    allowed.append(candidate)
                elif candidate.scope == "private" and candidate.owner == page_scope_context.owner:
                    allowed.append(candidate)
            if not allowed:
                raise ValueError(f"Source {source_id} is not accessible from {page_scope_context.scope} scope")
            if len(allowed) > 1:
                raise ValueError(
                    f"Source {source_id} is ambiguous from {page_scope_context.scope} scope; "
                    "backing source IDs must resolve to exactly one accessible source row"
                )
            resolved_page_ids.append(allowed[0].page_id)
        return sorted(set(resolved_page_ids))

    def find_entity_by_canonical_id(self, canonical_entity_id: str) -> dict[str, Any] | None:
        if not self.entities_data_source_id:
            return None
        result = self.client.query_data_source(
            self.entities_data_source_id,
            filter_obj={"property": "Canonical Entity ID", "rich_text": {"equals": canonical_entity_id}},
            page_size=1,
        )
        rows = result.get("results", [])
        return rows[0] if rows else None

    def resolve_entity_page_ids(self, entity_keys: list[str]) -> list[str]:
        if not self.entities_data_source_id or not entity_keys:
            return []
        result = self.client.query_data_source(
            self.entities_data_source_id,
            filter_obj={"or": [{"property": "Canonical Entity ID", "rich_text": {"equals": key}} for key in entity_keys]},
            page_size=max(25, len(entity_keys) * 2),
        )
        by_key = {
            self._entity_from_page(page).canonical_entity_id: self._entity_from_page(page).page_id
            for page in result.get("results", [])
        }
        return [by_key[key] for key in entity_keys if key in by_key]

    def upsert_entity(self, *, canonical_entity_id: str, name: str, entity_type: str) -> str:
        if not self.entities_data_source_id:
            raise ValueError("Entities data source is not configured")
        props = {
            "Entity Name": title_property(name),
            "Canonical Entity ID": rich_text_property(canonical_entity_id),
            "Entity Type": select_property(entity_type),
        }
        existing = self.find_entity_by_canonical_id(canonical_entity_id)
        if existing:
            self.client.update_page(existing["id"], props)
            return existing["id"]
        page = self.client.create_page(self.entities_data_source_id, props)
        return page["id"]

    def upsert_wiki_page(
        self,
        metadata: WikiPageMetadata,
        *,
        backing_source_page_ids: list[str],
        latest_job_page_id: str,
        related_entity_page_ids: list[str] | None = None,
    ) -> str:
        props = {
            "Wiki Title": title_property(metadata.title),
            "Wiki Slug": rich_text_property(metadata.slug),
            "Wiki Type": select_property(metadata.page_type),
            "Wiki Status": select_property(metadata.status),
            "Scope": select_property(metadata.scope),
            "Owner": rich_text_property(metadata.owner or ""),
            "Canonical Markdown Path": rich_text_property(metadata.path),
            "Summary": rich_text_property(metadata.summary),
            "Confidence Level": select_property(metadata.confidence),
            "Needs Human Review": checkbox_property(metadata.review_required),
            "Review State": select_property(metadata.review_state),
            "Last Generated At": date_property(now_iso()),
            "Backing Sources": relation_property(backing_source_page_ids),
            "Latest Job": relation_property([latest_job_page_id]),
            "Source Count": number_property(len(metadata.source_ids)),
        }
        if self.entities_data_source_id and related_entity_page_ids is not None:
            props["Related Entities"] = relation_property(related_entity_page_ids)
        existing = self.find_wiki_page_by_slug(metadata.slug, scope_context=metadata.scope_context)
        if existing:
            self.client.update_page(existing["id"], props)
            return existing["id"]
        page = self.client.create_page(self.wiki_data_source_id, props)
        return page["id"]

    def update_question_after_answer(
        self,
        question: QuestionRecord,
        *,
        latest_job_page_id: str,
        target_wiki_page_id: str | None,
        answer_page_slug: str | None,
        resolution_type: str,
    ) -> None:
        if not self.questions_data_source_id:
            raise ValueError("Questions data source is not configured")
        status = "answered" if resolution_type == "faq" else "queued"
        props = {
            "Question Status": select_property(status),
            "Scope": select_property(question.scope),
            "Owner": rich_text_property(question.owner or ""),
            "Latest Job": relation_property([latest_job_page_id]),
            "Answer Page Slug": rich_text_property(answer_page_slug or ""),
            "Resolution Type": select_property(resolution_type),
        }
        if target_wiki_page_id:
            props["Target Wiki Page"] = relation_property([target_wiki_page_id])
        self.client.update_page(question.page_id, props)

    def update_promotion_after_apply(self, promotion: PromotionRecord, *, latest_job_page_id: str) -> None:
        if not self.promotions_data_source_id:
            raise ValueError("Promotions data source is not configured")
        self.client.update_page(
            promotion.page_id,
            {
                "Status": select_property("applied"),
                "Latest Job": relation_property([latest_job_page_id]),
                "Scope": select_property(promotion.scope),
                "Owner": rich_text_property(promotion.owner or ""),
            },
        )
