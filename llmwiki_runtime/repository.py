from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any

from .models import JobRecord, SourceRecord, WikiPageMetadata
from .notion import (
    NotionClient,
    checkbox_property,
    date_property,
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


@dataclass
class NotionRepository:
    client: NotionClient
    sources_data_source_id: str
    wiki_data_source_id: str
    jobs_data_source_id: str
    policies_data_source_id: str

    def get_source(self, source_page_id: str) -> SourceRecord:
        page = self.client.retrieve_page(source_page_id)
        return SourceRecord(
            page_id=page["id"],
            source_id=_rich_text(page, "Source ID") or page["id"],
            source_type=_select(page, "Source Type") or "web_page",
            title=_title(page, "Source Title") or "Untitled Source",
            canonical_url=_url(page, "Canonical URL"),
            trust_level=_select(page, "Trust Level"),
            status=_select(page, "Source Status"),
            content_version=_number(page, "Content Version"),
            checksum=_rich_text(page, "Source Checksum") or None,
            trigger_regeneration=_checkbox(page, "Trigger Regeneration"),
            raw_text_pointer=_url(page, "Raw Text Pointer"),
            markdown_pointer=_url(page, "Normalised Markdown Pointer"),
            source_summary_pointer=_url(page, "Source Summary Pointer"),
            last_edited_time=page.get("last_edited_time"),
            properties=page["properties"],
        )

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
        jobs: list[JobRecord] = []
        for page in result.get("results", []):
            jobs.append(
                JobRecord(
                    page_id=page["id"],
                    job_id=_rich_text(page, "Job ID") or page["id"],
                    job_type=_select(page, "Job Type") or "",
                    status=_select(page, "Job Status") or "queued",
                    queue_timestamp=_date(page, "Queue Timestamp"),
                    target_source_page_id=(_relation_ids(page, "Target Source") or [None])[0],
                    target_wiki_page_id=(_relation_ids(page, "Target Wiki Page") or [None])[0],
                    idempotency_key=_rich_text(page, "Idempotency Key") or None,
                    policy_page_id=(_relation_ids(page, "Policy Version Ref") or [None])[0],
                    attempt_count=_number(page, "Attempt Count"),
                    properties=page["properties"],
                )
            )
        return jobs

    def claim_job(self, job: JobRecord, worker_name: str) -> str:
        started_at = now_iso()
        self.client.update_page(
            job.page_id,
            {
                "Job Status": select_property("running"),
                "Job Phase": select_property("running"),
                "Started At": date_property(started_at),
                "Locked": checkbox_property(True),
                "Worker Name": rich_text_property(worker_name),
            },
        )
        job.properties.setdefault("Started At", {"date": {"start": started_at}})
        return started_at

    def update_job_phase(self, page_id: str, phase: str) -> None:
        self.client.update_page(page_id, {"Job Phase": select_property(phase)})

    def mark_job_failed(self, page_id: str, error_class: str, message: str) -> None:
        finished_at = now_iso()
        self.client.update_page(
            page_id,
            {
                "Job Status": select_property("failed"),
                "Job Phase": select_property("running"),
                "Finished At": date_property(finished_at),
                "Error Class": select_property(error_class),
                "Error Message": rich_text_property(message[:1800]),
                "Locked": checkbox_property(False),
            },
        )

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
            },
        )

    def mark_source_failed(self, source: SourceRecord, message: str) -> None:
        now = now_iso()
        self.client.update_page(
            source.page_id,
            {
                "Source Status": select_property("failed"),
                "Parse Error": rich_text_property(message[:1800]),
                "Last Error At": date_property(now),
            },
        )

    def update_source_after_wiki(
        self,
        source: SourceRecord,
        *,
        source_summary_pointer: str,
    ) -> None:
        now = now_iso()
        self.client.update_page(
            source.page_id,
            {
                "Source Status": select_property("processed"),
                "Source Summary Pointer": url_property(source_summary_pointer),
                "Last Processed At": date_property(now),
                "Trigger Regeneration": checkbox_property(False),
            },
        )

    def find_existing_job_by_idempotency_key(self, key: str) -> JobRecord | None:
        result = self.client.query_data_source(
            self.jobs_data_source_id,
            filter_obj={"property": "Idempotency Key", "rich_text": {"equals": key}},
            page_size=1,
        )
        rows = result.get("results", [])
        if not rows:
            return None
        page = rows[0]
        return JobRecord(
            page_id=page["id"],
            job_id=_rich_text(page, "Job ID") or page["id"],
            job_type=_select(page, "Job Type") or "",
            status=_select(page, "Job Status") or "queued",
            queue_timestamp=_date(page, "Queue Timestamp"),
            target_source_page_id=(_relation_ids(page, "Target Source") or [None])[0],
            idempotency_key=_rich_text(page, "Idempotency Key") or None,
            properties=page["properties"],
        )

    def active_policy_page_id(self) -> str | None:
        result = self.client.query_data_source(
            self.policies_data_source_id,
            filter_obj={"property": "Active", "checkbox": {"equals": True}},
            page_size=1,
        )
        rows = result.get("results", [])
        return rows[0]["id"] if rows else None

    def create_job(
        self,
        *,
        job_type: str,
        title: str,
        target_source_page_id: str,
        idempotency_key: str,
        policy_page_id: str | None = None,
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
            "Trigger Type": select_property("manual"),
            "Priority": select_property("normal"),
            "Attempt Count": number_property(0),
            "Max Attempts": number_property(8),
            "Idempotency Key": rich_text_property(idempotency_key),
            "Locked": checkbox_property(False),
            "Target Source": relation_property([target_source_page_id]),
        }
        if policy_page_id:
            properties["Policy Version Ref"] = relation_property([policy_page_id])
        page = self.client.create_page(self.jobs_data_source_id, properties)
        return JobRecord(
            page_id=page["id"],
            job_id=job_id,
            job_type=job_type,
            status="queued",
            queue_timestamp=None,
            target_source_page_id=target_source_page_id,
            idempotency_key=idempotency_key,
            policy_page_id=policy_page_id,
            properties=page["properties"],
        )

    def find_wiki_page_by_slug(self, slug: str) -> dict[str, Any] | None:
        result = self.client.query_data_source(
            self.wiki_data_source_id,
            filter_obj={"property": "Wiki Slug", "rich_text": {"equals": slug}},
            page_size=1,
        )
        rows = result.get("results", [])
        return rows[0] if rows else None

    def upsert_wiki_page(
        self,
        metadata: WikiPageMetadata,
        *,
        source_page_id: str,
        latest_job_page_id: str,
    ) -> None:
        props = {
            "Wiki Title": title_property(metadata.title),
            "Wiki Slug": rich_text_property(metadata.slug),
            "Wiki Type": select_property(metadata.page_type),
            "Wiki Status": select_property(metadata.status),
            "Canonical Markdown Path": rich_text_property(metadata.path),
            "Summary": rich_text_property(metadata.summary),
            "Confidence Level": select_property(metadata.confidence),
            "Needs Human Review": checkbox_property(metadata.review_required),
            "Last Generated At": date_property(now_iso()),
            "Backing Sources": relation_property([source_page_id]),
            "Latest Job": relation_property([latest_job_page_id]),
            "Source Count": number_property(len(metadata.source_ids)),
        }
        existing = self.find_wiki_page_by_slug(metadata.slug)
        if existing:
            self.client.update_page(existing["id"], props)
        else:
            self.client.create_page(self.wiki_data_source_id, props)
