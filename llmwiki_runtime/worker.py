from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
from typing import Any

from .contracts import FILE_OPERATION_CONTRACT, maintainer_contract
from .frontmatter import dump_document, parse_document
from .llm import Planner
from .logging_utils import log_event
from .models import EXECUTABLE_JOB_TYPES, JobRecord, PolicyRecord, QuestionRecord, ScopeContext, SourceRecord
from .paths import ScopedPaths
from .repository import NotionRepository
from .sources import SourceFetcher
from .wiki_ops import (
    apply_run_plan,
    atomic_write_files,
    changed_files,
    derive_wiki_page_metadata,
    ensure_owner_scope,
    ensure_scope_root,
    ensure_wiki_root,
    load_candidate_pages,
    load_manifest,
    load_scope_pages,
    load_shared_overlay_pages,
    parse_run_plan,
    update_manifest,
    validate_run_plan,
    write_diff,
    write_run_record,
)


LOGGER = logging.getLogger(__name__)
REVIEW_STATE_ORDER = {"n_a": 0, "unreviewed": 1, "in_review": 2, "approved": 3}


class JobExecutionError(Exception):
    def __init__(self, error_class: str, message: str, *, output_pointer: str | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.output_pointer = output_pointer


@dataclass
class Worker:
    repository: NotionRepository
    source_fetcher: SourceFetcher
    planner: Planner | None
    wiki_root: Path
    worker_name: str

    def _ensure_scope(self, scope_context: ScopeContext) -> None:
        ensure_wiki_root(self.wiki_root)
        if scope_context.scope == "private":
            ensure_owner_scope(self.wiki_root, scope_context.owner or "")
        else:
            ensure_scope_root(self.wiki_root, scope_context)

    def _policy_payload(self, policy: PolicyRecord | None) -> dict[str, Any] | None:
        if policy is None:
            return None
        payload = asdict(policy)
        return payload

    def _load_policy(self, scope_context: ScopeContext) -> PolicyRecord | None:
        if hasattr(self.repository, "load_effective_policy"):
            return self.repository.load_effective_policy(scope_context)
        return None

    def enqueue_ingest_job(self, source_page_id: str) -> JobRecord:
        source = self.repository.get_source(source_page_id)
        self._ensure_scope(source.scope_context)
        suffix = source.checksum or source.last_edited_time or str(source.content_version or 0)
        key = f"{source.source_id}:{source.scope}:{source.owner or '-'}:ingest_source:{suffix}"
        policy_id = self.repository.active_policy_page_id(source.scope_context)
        job = self.repository.create_job(
            job_type="ingest_source",
            title=f"Ingest {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=key,
            scope_context=source.scope_context,
            policy_page_id=policy_id,
        )
        log_event(LOGGER, "job_enqueued", job_id=job.job_id, source_id=source.source_id, scope=source.scope, owner=source.owner, job_type="ingest_source")
        return job

    def enqueue_question_job(self, question_page_id: str) -> JobRecord:
        question = self.repository.get_question(question_page_id)
        self._ensure_scope(question.scope_context)
        key = f"{question.question_id}:{question.scope}:{question.owner or '-'}:answer_question"
        policy_id = self.repository.active_policy_page_id(question.scope_context)
        return self.repository.create_job(
            job_type="answer_question",
            title=f"Answer {question.question[:80]}",
            target_question_page_id=question.page_id,
            idempotency_key=key,
            scope_context=question.scope_context,
            policy_page_id=policy_id,
        )

    def enqueue_promotion_job(self, promotion_page_id: str) -> JobRecord:
        promotion = self.repository.get_promotion(promotion_page_id)
        self._ensure_scope(ScopeContext("shared"))
        key = f"{promotion.promotion_id}:{promotion.scope}:{promotion.owner or '-'}:promote_private:{promotion.status}"
        policy_id = self.repository.active_policy_page_id(ScopeContext("shared"))
        return self.repository.create_job(
            job_type="promote_private",
            title=f"Promote {promotion.promotion_id}",
            target_promotion_page_id=promotion.page_id,
            idempotency_key=key,
            scope_context=ScopeContext("shared"),
            policy_page_id=policy_id,
        )

    def _create_job(self, **kwargs):
        return self.repository.create_job(**kwargs)

    def _update_source_after_wiki(self, source_record: SourceRecord, *, source_summary_pointer: str, related_entity_page_ids: list[str]) -> None:
        self.repository.update_source_after_wiki(
            source_record,
            source_summary_pointer=source_summary_pointer,
            related_entity_page_ids=related_entity_page_ids,
        )

    def _upsert_wiki_page(self, metadata, *, backing_source_page_ids: list[str], latest_job_page_id: str, related_entity_page_ids: list[str]) -> str:
        result = self.repository.upsert_wiki_page(
            metadata,
            backing_source_page_ids=backing_source_page_ids,
            latest_job_page_id=latest_job_page_id,
            related_entity_page_ids=related_entity_page_ids,
        )
        return metadata.path if result is None else result

    def run_once(self) -> JobRecord | None:
        for job in self.repository.query_queued_jobs():
            if job.job_type not in EXECUTABLE_JOB_TYPES:
                continue
            self.run_job(job)
            return job
        return None

    def run_job(self, job: JobRecord) -> None:
        log_event(LOGGER, "job_claiming", job_id=job.job_id, job_type=job.job_type, scope=job.scope, owner=job.owner)
        started_at = self.repository.claim_job(job, self.worker_name)
        if started_at is None:
            log_event(LOGGER, "job_claim_lost", job_id=job.job_id, job_type=job.job_type, scope=job.scope, owner=job.owner)
            return
        try:
            if job.job_type == "ingest_source":
                self._run_ingest_job(job, started_at)
                return
            if job.job_type == "update_wiki":
                self._run_update_wiki_job(job, started_at)
                return
            if job.job_type == "answer_question":
                self._run_answer_question_job(job, started_at)
                return
            if job.job_type == "promote_private":
                self._run_promote_private_job(job, started_at)
                return
            raise ValueError(f"Unsupported job type: {job.job_type}")
        except ValueError as exc:
            self._mark_failure(job, "validation", str(exc))
        except JobExecutionError as exc:
            self._mark_failure(job, exc.error_class, str(exc), output_pointer=exc.output_pointer)
        except OSError as exc:
            self._mark_failure(job, "external_io", str(exc))
        except Exception as exc:  # pragma: no cover
            self._mark_failure(job, "unknown", str(exc))
            raise

    def _mark_failure(self, job: JobRecord, error_class: str, message: str, *, output_pointer: str | None = None) -> None:
        if job.target_source_page_id:
            self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), message)
        self.repository.mark_job_failed(job.page_id, error_class, message, output_pointer=output_pointer)
        log_event(LOGGER, "job_failed", job_id=job.job_id, error_class=error_class, message=message, output_pointer=output_pointer)

    def _coerce_review_state(self, current: str, minimum: str | None) -> str:
        if not minimum:
            return current
        if current == "rejected":
            return current
        return max(current, minimum, key=lambda value: REVIEW_STATE_ORDER.get(value, -1))

    def _enforce_policy(
        self,
        *,
        policy: PolicyRecord | None,
        metadata_by_path: dict[str, Any],
        job_type: str,
        resolution_type: str | None = None,
    ) -> None:
        if policy is None:
            return
        allowed_page_types = set(policy.allowed_page_types)
        if allowed_page_types:
            for metadata in metadata_by_path.values():
                if metadata.page_type not in allowed_page_types:
                    raise ValueError(f"Policy disallows page type {metadata.page_type}")
        if policy.entity_extraction == "off":
            for metadata in metadata_by_path.values():
                if metadata.page_type == "entity":
                    raise ValueError("Policy disables entity extraction")
        if job_type == "answer_question" and resolution_type:
            if policy.question_mode == "open_question" and resolution_type != "open_question":
                raise ValueError("Policy only permits open_question resolution")
            if policy.question_mode == "faq" and resolution_type != "faq":
                raise ValueError("Policy only permits faq resolution")
        for metadata in metadata_by_path.values():
            if metadata.scope == "shared":
                if policy.promotion_required_for_shared and any(scope != "shared" for scope in metadata.source_scope):
                    raise ValueError("Policy requires promotion for shared pages with private provenance")
                if policy.requires_human_review:
                    metadata.review_required = True
                    metadata.review_state = self._coerce_review_state(metadata.review_state, policy.minimum_review_state_for_shared or "unreviewed")
                    if metadata.status == "published" and not policy.auto_publish_allowed:
                        metadata.status = "draft"
                elif policy.minimum_review_state_for_shared:
                    metadata.review_state = self._coerce_review_state(metadata.review_state, policy.minimum_review_state_for_shared)
                if policy.max_source_count is not None and len(metadata.source_ids) > policy.max_source_count:
                    raise ValueError(f"Policy max_source_count exceeded for {metadata.path}")

    def _existing_pages_for_source_scope(self, source: SourceRecord, scoped_paths: ScopedPaths, manifest: dict[str, Any] | None) -> dict[str, str | None]:
        pages = load_candidate_pages(scoped_paths, source.source_id, manifest)
        if source.scope == "private":
            pages.update(load_shared_overlay_pages(self.wiki_root))
        return dict(sorted(pages.items()))

    def _build_source_bundle(self, job: JobRecord, source: SourceRecord, scoped_paths: ScopedPaths, artifacts_dir: Path) -> dict[str, Any]:
        manifest = load_manifest(scoped_paths, source.source_id)
        metadata_path = artifacts_dir / "metadata.json"
        markdown_path = artifacts_dir / "source.md"
        policy = self._load_policy(source.scope_context)
        return {
            "job": {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "source_id": source.source_id,
                "source_page_id": source.page_id,
                "policy_page_id": job.policy_page_id,
                "scope": source.scope,
                "owner": source.owner,
                "raw_source_dir": scoped_paths.relative(artifacts_dir),
                "manifest_path": scoped_paths.relative(scoped_paths.manifest_path(source.source_id)),
            },
            "source": {
                "source_id": source.source_id,
                "scope": source.scope,
                "owner": source.owner,
                "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
                "content_markdown": markdown_path.read_text(encoding="utf-8"),
            },
            "current_manifest": manifest,
            "existing_pages": self._existing_pages_for_source_scope(source, scoped_paths, manifest),
            "maintainer_contract": maintainer_contract(source.scope_context),
            "operation_schema": FILE_OPERATION_CONTRACT,
            "effective_policy": self._policy_payload(policy),
        }

    def _build_question_bundle(self, job: JobRecord, question: QuestionRecord) -> dict[str, Any]:
        scoped_paths = ScopedPaths(self.wiki_root, question.scope_context)
        existing_pages = load_scope_pages(scoped_paths)
        if question.scope == "private":
            existing_pages.update(load_shared_overlay_pages(self.wiki_root))
        policy = self._load_policy(question.scope_context)
        return {
            "job": {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "question_id": question.question_id,
                "question_page_id": question.page_id,
                "policy_page_id": job.policy_page_id,
                "scope": question.scope,
                "owner": question.owner,
            },
            "question_context": {
                "question_id": question.question_id,
                "question": question.question,
                "scope": question.scope,
                "owner": question.owner,
            },
            "existing_pages": dict(sorted(existing_pages.items())),
            "maintainer_contract": maintainer_contract(question.scope_context),
            "operation_schema": FILE_OPERATION_CONTRACT,
            "effective_policy": self._policy_payload(policy),
        }

    def _promotion_candidate_path(self, promotion_id: str) -> Path:
        return self.wiki_root / "reviews" / "promotion_queue" / f"{promotion_id}.json"

    def _promotion_result_path(self, status: str, promotion_id: str) -> Path:
        return self.wiki_root / "reviews" / status / f"{promotion_id}.json"

    def _promotion_log_path(self, promotion_id: str) -> Path:
        return self.wiki_root / "state" / "promotion_logs" / f"{promotion_id}.json"

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _build_promotion_bundle(self, job: JobRecord, promotion) -> tuple[dict[str, Any], Path]:
        if not promotion.source_private_page_id:
            raise ValueError("Promotion is missing Source Private Page relation")
        source_page = self.repository.get_wiki_page(promotion.source_private_page_id)
        if source_page.scope != "private" or source_page.owner != promotion.owner:
            raise ValueError("Promotion source page must be a private page owned by the promotion owner")
        source_path = self.wiki_root / source_page.path
        if not source_path.exists():
            raise ValueError(f"Promotion source page does not exist on disk: {source_page.path}")
        source_content = source_path.read_text(encoding="utf-8")
        candidate = {
            "promotion_id": promotion.promotion_id,
            "source_private_page_path": source_page.path,
            "source_private_slug": source_page.slug,
            "owner": promotion.owner,
            "status": promotion.status,
            "decision": promotion.decision,
            "target_shared_page_ids": promotion.target_shared_page_ids,
            "claims_preview": parse_document(source_content).body.splitlines()[:20],
        }
        candidate_path = self._write_json_file(self._promotion_candidate_path(promotion.promotion_id), candidate)
        shared_scope = ScopeContext("shared")
        bundle = {
            "job": {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "promotion_id": promotion.promotion_id,
                "promotion_page_id": promotion.page_id,
                "policy_page_id": job.policy_page_id,
                "scope": "shared",
                "owner": None,
            },
            "promotion_context": {
                "promotion_id": promotion.promotion_id,
                "owner": promotion.owner,
                "submitted_by": promotion.submitted_by,
                "reviewed_by": promotion.reviewed_by,
                "source_private_page_path": source_page.path,
                "source_private_content": source_content,
                "target_shared_pages": [
                    {
                        "page_id": page_id,
                        "metadata": asdict(self.repository.get_wiki_page(page_id)),
                    }
                    for page_id in promotion.target_shared_page_ids
                ],
                "candidate_path": str(candidate_path),
            },
            "existing_pages": dict(sorted(load_scope_pages(ScopedPaths(self.wiki_root, shared_scope)).items())),
            "maintainer_contract": maintainer_contract(shared_scope),
            "operation_schema": FILE_OPERATION_CONTRACT,
            "effective_policy": self._policy_payload(self._load_policy(shared_scope)),
        }
        return bundle, candidate_path

    def _sync_wiki_pages(
        self,
        *,
        job: JobRecord,
        metadata_by_path: dict[str, Any],
    ) -> dict[str, str]:
        entity_page_ids_by_key: dict[str, str] = {}
        for metadata in metadata_by_path.values():
            if metadata.page_type == "entity" and metadata.scope == "shared":
                canonical_entity_id = metadata.entity_keys[0] if metadata.entity_keys else metadata.slug
                entity_page_ids_by_key[canonical_entity_id] = self.repository.upsert_entity(
                    canonical_entity_id=canonical_entity_id,
                    name=metadata.title,
                    entity_type=metadata.entity_type or "concept",
                )
        wiki_page_ids_by_path: dict[str, str] = {}
        for metadata in metadata_by_path.values():
            related_entity_page_ids = []
            if metadata.entity_keys:
                for key in metadata.entity_keys:
                    if key in entity_page_ids_by_key:
                        related_entity_page_ids.append(entity_page_ids_by_key[key])
                unresolved_keys = [key for key in metadata.entity_keys if key not in entity_page_ids_by_key]
                existing_entity_ids = []
                if unresolved_keys and hasattr(self.repository, "resolve_entity_page_ids"):
                    existing_entity_ids = self.repository.resolve_entity_page_ids(unresolved_keys)
                related_entity_page_ids.extend(existing_entity_ids)
                related_entity_page_ids = sorted(set(related_entity_page_ids))
            backing_source_page_ids = self.repository.resolve_backing_source_page_ids(
                metadata.source_ids,
                page_scope_context=metadata.scope_context,
            )
            wiki_page_ids_by_path[metadata.path] = self._upsert_wiki_page(
                metadata,
                backing_source_page_ids=backing_source_page_ids,
                latest_job_page_id=job.page_id,
                related_entity_page_ids=related_entity_page_ids,
            )
        return wiki_page_ids_by_path

    def _apply_metadata_overrides(self, state: dict[str, str], metadata_by_path: dict[str, Any]) -> None:
        for path, metadata in metadata_by_path.items():
            parsed = parse_document(state[path])
            parsed.metadata["status"] = metadata.status
            parsed.metadata["confidence"] = metadata.confidence
            parsed.metadata["review_required"] = metadata.review_required
            parsed.metadata["review_state"] = metadata.review_state
            parsed.metadata["promotion_origin"] = metadata.promotion_origin
            state[path] = dump_document(parsed.metadata, parsed.body)

    def _persist_failure_record(
        self,
        *,
        scope_context: ScopeContext,
        job_id: str,
        raw_model_output: str,
        failure: dict[str, Any],
        plan=None,
    ) -> str:
        scoped_paths = ScopedPaths(self.wiki_root, scope_context)
        record_path = write_run_record(
            scoped_paths=scoped_paths,
            job_id=job_id,
            raw_model_output=raw_model_output,
            plan=plan,
            failure=failure,
        )
        return record_path.as_uri()

    def _run_ingest_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_source_page_id:
            raise ValueError("ingest_source job is missing Target Source relation")
        self._ensure_scope(job.scope_context)
        source = self.repository.get_source(job.target_source_page_id)
        if source.scope != job.scope or source.owner != job.owner:
            raise ValueError("Source scope/owner does not match job scope/owner")
        self.repository.mark_source_fetching(source)
        artifacts = self.source_fetcher.fetch(source)
        raw_text_pointer = (artifacts.storage_dir / "source.txt").as_uri()
        markdown_pointer = (artifacts.storage_dir / "source.md").as_uri()
        self.repository.update_source_for_ingest(source, checksum=artifacts.checksum, raw_text_pointer=raw_text_pointer, markdown_pointer=markdown_pointer)
        update_key = f"{source.source_id}:{source.scope}:{source.owner or '-'}:update_wiki:{artifacts.checksum}"
        self._create_job(
            job_type="update_wiki",
            title=f"Update wiki from {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=update_key,
            scope_context=source.scope_context,
            policy_page_id=self.repository.active_policy_page_id(source.scope_context),
            trigger_type="dependency",
        )
        self.repository.mark_job_succeeded(job.page_id, started_at=started_at, output_pointer=markdown_pointer, diff_pointer=None)
        log_event(LOGGER, "ingest_succeeded", job_id=job.job_id, source_id=source.source_id, scope=source.scope, owner=source.owner, checksum=artifacts.checksum)

    def _run_update_wiki_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_source_page_id:
            raise ValueError("update_wiki job is missing Target Source relation")
        if self.planner is None:
            raise ValueError("LLM planner is not configured")
        self._ensure_scope(job.scope_context)
        source = self.repository.get_source(job.target_source_page_id)
        if source.scope != job.scope or source.owner != job.owner:
            raise ValueError("Source scope/owner does not match job scope/owner")
        scoped_paths = ScopedPaths(self.wiki_root, source.scope_context)
        artifacts_dir = scoped_paths.source_artifact_dir(source.source_id)
        if not (artifacts_dir / "source.md").exists():
            raise ValueError(f"Missing source artefacts for {source.source_id}")
        bundle = self._build_source_bundle(job, source, scoped_paths, artifacts_dir)
        policy = self._load_policy(source.scope_context)
        self._run_planned_wiki_job(
            job=job,
            started_at=started_at,
            scope_context=source.scope_context,
            bundle=bundle,
            policy=policy,
            current_source_id=source.source_id,
            current_source_scope=source.scope,
            require_canonical_source_page=True,
            source_summary_pointer=(self.wiki_root / scoped_paths.relative(scoped_paths.source_page_path(source.source_id))).as_uri(),
            source_record=source,
        )

    def _run_answer_question_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_question_page_id:
            raise ValueError("answer_question job is missing Target Question relation")
        if self.planner is None:
            raise ValueError("LLM planner is not configured")
        question = self.repository.get_question(job.target_question_page_id)
        if question.scope != job.scope or question.owner != job.owner:
            raise ValueError("Question scope/owner does not match job scope/owner")
        self._ensure_scope(question.scope_context)
        policy = self._load_policy(question.scope_context)
        bundle = self._build_question_bundle(job, question)
        metadata_by_path, _, _, wiki_page_ids_by_path, dry_run = self._run_planned_wiki_job(
            job=job,
            started_at=started_at,
            scope_context=question.scope_context,
            bundle=bundle,
            policy=policy,
            current_source_id=None,
            current_source_scope=None,
            require_canonical_source_page=False,
        )
        if dry_run:
            return
        answer_metadata = None
        for metadata in metadata_by_path.values():
            if metadata.page_type == "faq":
                answer_metadata = metadata
                break
            if metadata.page_type == "question":
                answer_metadata = metadata
        if answer_metadata is None:
            raise ValueError("answer_question must produce an faq or question page")
        resolution_type = "faq" if answer_metadata.page_type == "faq" else "open_question"
        self.repository.update_question_after_answer(
            question,
            latest_job_page_id=job.page_id,
            target_wiki_page_id=wiki_page_ids_by_path.get(answer_metadata.path),
            answer_page_slug=answer_metadata.slug,
            resolution_type=resolution_type,
        )

    def _run_promote_private_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_promotion_page_id:
            raise ValueError("promote_private job is missing Target Promotion relation")
        if self.planner is None:
            raise ValueError("LLM planner is not configured")
        promotion = self.repository.get_promotion(job.target_promotion_page_id)
        if promotion.status != "approved":
            raise ValueError("Only approved promotions may be applied")
        bundle, candidate_path = self._build_promotion_bundle(job, promotion)
        policy = self._load_policy(ScopeContext("shared"))
        _, changed, run_record_path, _, dry_run = self._run_planned_wiki_job(
            job=job,
            started_at=started_at,
            scope_context=ScopeContext("shared"),
            bundle=bundle,
            policy=policy,
            current_source_id=None,
            current_source_scope=None,
            require_canonical_source_page=False,
        )
        if dry_run:
            return
        summary = {
            "promotion_id": promotion.promotion_id,
            "candidate_path": str(candidate_path),
            "run_record_path": str(run_record_path),
            "changed_paths": sorted(changed),
        }
        self._write_json_file(self._promotion_result_path("approved", promotion.promotion_id), summary)
        self._write_json_file(self._promotion_log_path(promotion.promotion_id), summary)
        self.repository.update_promotion_after_apply(promotion, latest_job_page_id=job.page_id)

    def _run_planned_wiki_job(
        self,
        *,
        job: JobRecord,
        started_at: str,
        scope_context: ScopeContext,
        bundle: dict[str, Any],
        policy: PolicyRecord | None,
        current_source_id: str | None,
        current_source_scope: str | None,
        require_canonical_source_page: bool,
        source_summary_pointer: str | None = None,
        source_record: SourceRecord | None = None,
    ) -> tuple[dict[str, Any], dict[str, tuple[str, str]], Path, dict[str, str], bool]:
        raw_model_output = self.planner.plan(bundle) if self.planner is not None else ""
        log_event(LOGGER, "planner_output_received", job_id=job.job_id, scope=job.scope, owner=job.owner)
        self.repository.update_job_phase(job.page_id, "validating_plan")
        scoped_paths = ScopedPaths(self.wiki_root, scope_context)
        plan = None
        try:
            plan = parse_run_plan(raw_model_output)
            if plan.job_id != job.job_id:
                raise ValueError("Planner returned mismatched job_id")
            if current_source_id and plan.source_id != current_source_id:
                raise ValueError("Planner returned mismatched source_id")
            validate_run_plan(plan, root=self.wiki_root, scope_context=scope_context)
            state = apply_run_plan(
                plan,
                root=self.wiki_root,
                scope_context=scope_context,
                current_source_id=current_source_id,
                current_source_scope=current_source_scope,
            )
            metadata_by_path = {relative_path: derive_wiki_page_metadata(relative_path, state[relative_path]) for relative_path in plan.touched_paths}
            if require_canonical_source_page and current_source_id:
                source_page_path = scoped_paths.relative(scoped_paths.source_page_path(current_source_id))
                if source_page_path not in state and not (self.wiki_root / source_page_path).exists():
                    raise ValueError(
                        f"Canonical source page {source_page_path} must exist after update_wiki; first successful runs may not be no_op"
                    )
            resolution_type = None
            if job.job_type == "answer_question":
                resolution_type = "faq" if any(metadata.page_type == "faq" for metadata in metadata_by_path.values()) else "open_question"
            self._enforce_policy(policy=policy, metadata_by_path=metadata_by_path, job_type=job.job_type, resolution_type=resolution_type)
            self._apply_metadata_overrides(state, metadata_by_path)
            self.repository.update_job_phase(job.page_id, "applying_changes")
            changed = changed_files(plan, state, root=self.wiki_root)
            if plan.run_mode == "dry_run":
                run_record_path = write_run_record(
                    scoped_paths=scoped_paths,
                    job_id=job.job_id,
                    raw_model_output=raw_model_output,
                    plan=plan,
                    changed=changed,
                    manifest_path=None,
                    dry_run=True,
                )
                self.repository.update_job_phase(job.page_id, "syncing_state")
                self.repository.mark_job_succeeded(
                    job.page_id,
                    started_at=started_at,
                    output_pointer=run_record_path.as_uri(),
                    diff_pointer=None,
                )
                return metadata_by_path, changed, run_record_path, {}, True
            atomic_write_files(changed, root=self.wiki_root)
            diff_path = write_diff(job.job_id, changed=changed, scoped_paths=scoped_paths)
            manifest_path = None
            if current_source_id:
                metadata_file = scoped_paths.source_artifact_dir(current_source_id) / "metadata.json"
                manifest_path = update_manifest(
                    scoped_paths=scoped_paths,
                    source_id=current_source_id,
                    checksum=json.loads(metadata_file.read_text(encoding="utf-8"))["checksum"],
                    source_page=plan.manifest_update["source_page"],
                    affected_pages=plan.manifest_update["affected_pages"],
                    job_id=job.job_id,
                )
            run_record_path = write_run_record(
                scoped_paths=scoped_paths,
                job_id=job.job_id,
                raw_model_output=raw_model_output,
                plan=plan,
                changed=changed,
                manifest_path=manifest_path,
            )
            self.repository.update_job_phase(job.page_id, "syncing_state")
            wiki_page_ids_by_path = self._sync_wiki_pages(job=job, metadata_by_path=metadata_by_path)
            if source_record is not None and source_summary_pointer is not None and current_source_id:
                source_page_path = scoped_paths.relative(scoped_paths.source_page_path(current_source_id))
                source_metadata = metadata_by_path.get(source_page_path)
                related_entity_ids = []
                if source_metadata and source_metadata.entity_keys:
                    related_entity_ids = self.repository.resolve_entity_page_ids(source_metadata.entity_keys)
                self._update_source_after_wiki(
                    source_record,
                    source_summary_pointer=source_summary_pointer,
                    related_entity_page_ids=related_entity_ids,
                )
            self.repository.mark_job_succeeded(
                job.page_id,
                started_at=started_at,
                output_pointer=run_record_path.as_uri(),
                diff_pointer=diff_path.as_uri(),
            )
            return metadata_by_path, changed, run_record_path, wiki_page_ids_by_path, False
        except (ValueError, JobExecutionError) as exc:
            output_pointer = self._persist_failure_record(
                scope_context=scope_context,
                job_id=job.job_id,
                raw_model_output=raw_model_output,
                failure={"stage": "planner", "error": str(exc), "error_class": "validation", "message": str(exc)},
                plan=plan,
            )
            raise JobExecutionError("validation", str(exc), output_pointer=output_pointer) from exc
