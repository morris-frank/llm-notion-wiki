from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from .contracts import FILE_OPERATION_CONTRACT, maintainer_contract
from .llm import Planner
from .logging_utils import log_event
from .models import EXECUTABLE_JOB_TYPES, JobRecord, ScopeContext, SourceRecord
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
    load_shared_overlay_pages,
    parse_run_plan,
    update_manifest,
    validate_run_plan,
    write_diff,
    write_run_record,
)


LOGGER = logging.getLogger(__name__)


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

    def enqueue_ingest_job(self, source_page_id: str) -> JobRecord:
        source = self.repository.get_source(source_page_id)
        self._ensure_scope(source.scope_context)
        version = source.content_version or 0
        suffix = source.checksum or source.last_edited_time or str(version)
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
        log_event(
            LOGGER,
            "job_enqueued",
            job_id=job.job_id,
            source_id=source.source_id,
            scope=source.scope,
            owner=source.owner,
            job_type="ingest_source",
        )
        return job

    def run_once(self) -> JobRecord | None:
        for job in self.repository.query_queued_jobs():
            if job.job_type not in EXECUTABLE_JOB_TYPES:
                continue
            self.run_job(job)
            return job
        return None

    def run_job(self, job: JobRecord) -> None:
        log_event(
            LOGGER,
            "job_claiming",
            job_id=job.job_id,
            job_type=job.job_type,
            scope=job.scope,
            owner=job.owner,
        )
        started_at = self.repository.claim_job(job, self.worker_name)
        if started_at is None:
            log_event(
                LOGGER,
                "job_claim_lost",
                job_id=job.job_id,
                job_type=job.job_type,
                scope=job.scope,
                owner=job.owner,
            )
            return
        try:
            if job.job_type == "ingest_source":
                self._run_ingest_job(job, started_at)
                return
            if job.job_type == "update_wiki":
                self._run_update_wiki_job(job, started_at)
                return
            raise ValueError(f"Unsupported job type: {job.job_type}")
        except ValueError as exc:
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, "validation", str(exc))
            log_event(LOGGER, "job_failed", job_id=job.job_id, error_class="validation", message=str(exc))
        except JobExecutionError as exc:
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, exc.error_class, str(exc), output_pointer=exc.output_pointer)
            log_event(
                LOGGER,
                "job_failed",
                job_id=job.job_id,
                error_class=exc.error_class,
                message=str(exc),
                output_pointer=exc.output_pointer,
            )
        except OSError as exc:
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, "external_io", str(exc))
            log_event(LOGGER, "job_failed", job_id=job.job_id, error_class="external_io", message=str(exc))
        except Exception as exc:  # pragma: no cover - defensive top-level guard
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, "unknown", str(exc))
            log_event(LOGGER, "job_failed", job_id=job.job_id, error_class="unknown", message=str(exc))
            raise

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
        self.repository.update_source_for_ingest(
            source,
            checksum=artifacts.checksum,
            raw_text_pointer=raw_text_pointer,
            markdown_pointer=markdown_pointer,
        )
        update_key = f"{source.source_id}:{source.scope}:{source.owner or '-'}:update_wiki:{artifacts.checksum}"
        policy_id = self.repository.active_policy_page_id(source.scope_context)
        self.repository.create_job(
            job_type="update_wiki",
            title=f"Update wiki from {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=update_key,
            scope_context=source.scope_context,
            policy_page_id=policy_id,
        )
        self.repository.mark_job_succeeded(
            job.page_id,
            started_at=started_at,
            output_pointer=markdown_pointer,
            diff_pointer=None,
        )
        log_event(
            LOGGER,
            "ingest_succeeded",
            job_id=job.job_id,
            source_id=source.source_id,
            scope=source.scope,
            owner=source.owner,
            checksum=artifacts.checksum,
        )

    def _build_llm_bundle(
        self,
        job: JobRecord,
        source: SourceRecord,
        scoped_paths: ScopedPaths,
        artifacts_dir: Path,
    ) -> dict[str, Any]:
        manifest = load_manifest(scoped_paths, source.source_id)
        metadata_path = artifacts_dir / "metadata.json"
        markdown_path = artifacts_dir / "source.md"
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
            "existing_pages": self._existing_pages_for_scope(source, scoped_paths, manifest),
            "maintainer_contract": maintainer_contract(source.scope_context),
            "operation_schema": FILE_OPERATION_CONTRACT,
        }

    def _existing_pages_for_scope(
        self,
        source: SourceRecord,
        scoped_paths: ScopedPaths,
        manifest: dict[str, Any] | None,
    ) -> dict[str, str | None]:
        pages = load_candidate_pages(scoped_paths, source.source_id, manifest)
        if source.scope == "private":
            pages.update(load_shared_overlay_pages(self.wiki_root))
        return dict(sorted(pages.items()))

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
        bundle = self._build_llm_bundle(job, source, scoped_paths, artifacts_dir)
        raw_model_output = self.planner.plan(bundle)
        log_event(
            LOGGER,
            "planner_output_received",
            job_id=job.job_id,
            source_id=source.source_id,
            scope=source.scope,
            owner=source.owner,
        )
        self.repository.update_job_phase(job.page_id, "validating_plan")
        plan = None
        changed: dict[str, tuple[str, str]] = {}
        failure_stage = "validating_plan"
        try:
            plan = parse_run_plan(raw_model_output)
            if plan.job_id != job.job_id:
                raise ValueError("Planner returned mismatched job_id")
            if plan.source_id != source.source_id:
                raise ValueError("Planner returned mismatched source_id")
            validate_run_plan(plan, root=self.wiki_root, scope_context=source.scope_context)
            state = apply_run_plan(
                plan,
                root=self.wiki_root,
                scope_context=source.scope_context,
                source_scope=source.scope,
            )
            metadata_by_path: dict[str, tuple[Any, list[str]]] = {}
            for relative_path in plan.touched_paths:
                metadata = derive_wiki_page_metadata(relative_path, state[relative_path])
                backing_source_page_ids = self.repository.resolve_backing_source_page_ids(
                    metadata.source_ids,
                    page_scope_context=metadata.scope_context,
                )
                metadata_by_path[relative_path] = (metadata, backing_source_page_ids)
            source_page_path = scoped_paths.relative(scoped_paths.source_page_path(source.source_id))
            canonical_source_page_exists = source_page_path in state or (self.wiki_root / source_page_path).exists()
            if not canonical_source_page_exists:
                raise ValueError(
                    f"Canonical source page {source_page_path} must exist after update_wiki; "
                    "first successful runs may not be no_op"
                )
            self.repository.update_job_phase(job.page_id, "applying_changes")
            failure_stage = "applying_changes"
            changed = changed_files(plan, state, root=self.wiki_root)
            atomic_write_files(changed, root=self.wiki_root)
            diff_path = write_diff(job.job_id, changed=changed, scoped_paths=scoped_paths)
            manifest_path = update_manifest(
                scoped_paths=scoped_paths,
                source_id=source.source_id,
                checksum=json.loads((artifacts_dir / "metadata.json").read_text(encoding="utf-8"))["checksum"],
                source_page=source_page_path,
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
            for relative_path in changed:
                metadata, backing_source_page_ids = metadata_by_path[relative_path]
                self.repository.upsert_wiki_page(
                    metadata,
                    backing_source_page_ids=backing_source_page_ids,
                    latest_job_page_id=job.page_id,
                )
            self.repository.update_source_after_wiki(
                source,
                source_summary_pointer=(self.wiki_root / source_page_path).as_uri(),
            )
            self.repository.mark_job_succeeded(
                job.page_id,
                started_at=started_at,
                output_pointer=run_record_path.as_uri(),
                diff_pointer=diff_path.as_uri(),
            )
            log_event(
                LOGGER,
                "wiki_update_succeeded",
                job_id=job.job_id,
                source_id=source.source_id,
                scope=source.scope,
                owner=source.owner,
                changed_files=sorted(changed),
            )
        except Exception as exc:
            error_class = "validation" if isinstance(exc, ValueError) else "external_io" if isinstance(exc, OSError) else "unknown"
            run_record_path = write_run_record(
                scoped_paths=scoped_paths,
                job_id=job.job_id,
                raw_model_output=raw_model_output,
                plan=plan,
                changed=changed,
                manifest_path=None,
                failure={
                    "stage": failure_stage,
                    "error_class": error_class,
                    "message": str(exc),
                },
            )
            raise JobExecutionError(error_class, str(exc), output_pointer=run_record_path.as_uri()) from exc
