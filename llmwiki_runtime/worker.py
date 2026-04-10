from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .contracts import FILE_OPERATION_CONTRACT, MAINTAINER_CONTRACT
from .llm import Planner
from .models import EXECUTABLE_JOB_TYPES, JobRecord, SourceRecord
from .repository import NotionRepository
from .sources import SourceFetcher
from .wiki_ops import (
    apply_run_plan,
    atomic_write_files,
    changed_files,
    derive_wiki_page_metadata,
    ensure_wiki_root,
    load_candidate_pages,
    load_manifest,
    parse_run_plan,
    update_manifest,
    validate_run_plan,
    write_diff,
    write_run_record,
)


@dataclass
class Worker:
    repository: NotionRepository
    source_fetcher: SourceFetcher
    planner: Planner | None
    wiki_root: Path
    worker_name: str

    def enqueue_ingest_job(self, source_page_id: str) -> JobRecord:
        source = self.repository.get_source(source_page_id)
        version = source.content_version or 0
        suffix = source.checksum or source.last_edited_time or str(version)
        key = f"{source.source_id}:ingest_source:{suffix}"
        policy_id = self.repository.active_policy_page_id()
        return self.repository.create_job(
            job_type="ingest_source",
            title=f"Ingest {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=key,
            policy_page_id=policy_id,
        )

    def run_once(self) -> JobRecord | None:
        for job in self.repository.query_queued_jobs():
            if job.job_type not in EXECUTABLE_JOB_TYPES:
                continue
            self.run_job(job)
            return job
        return None

    def run_job(self, job: JobRecord) -> None:
        started_at = self.repository.claim_job(job, self.worker_name)
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
        except OSError as exc:
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, "external_io", str(exc))
        except Exception as exc:  # pragma: no cover - defensive top-level guard
            if job.target_source_page_id:
                self.repository.mark_source_failed(self.repository.get_source(job.target_source_page_id), str(exc))
            self.repository.mark_job_failed(job.page_id, "unknown", str(exc))

    def _run_ingest_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_source_page_id:
            raise ValueError("ingest_source job is missing Target Source relation")
        ensure_wiki_root(self.wiki_root)
        source = self.repository.get_source(job.target_source_page_id)
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
        update_key = f"{source.source_id}:update_wiki:{artifacts.checksum}"
        policy_id = self.repository.active_policy_page_id()
        self.repository.create_job(
            job_type="update_wiki",
            title=f"Update wiki from {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=update_key,
            policy_page_id=policy_id,
        )
        self.repository.mark_job_succeeded(
            job.page_id,
            started_at=started_at,
            output_pointer=markdown_pointer,
            diff_pointer=None,
        )

    def _build_llm_bundle(self, job: JobRecord, source: SourceRecord, artifacts_dir: Path) -> dict[str, Any]:
        manifest = load_manifest(self.wiki_root, source.source_id)
        metadata_path = artifacts_dir / "metadata.json"
        markdown_path = artifacts_dir / "source.md"
        bundle = {
            "job": {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "source_id": source.source_id,
                "source_page_id": source.page_id,
                "policy_page_id": job.policy_page_id,
            },
            "source": {
                "source_id": source.source_id,
                "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
                "content_markdown": markdown_path.read_text(encoding="utf-8"),
            },
            "current_manifest": manifest,
            "existing_pages": load_candidate_pages(self.wiki_root, source.source_id, manifest),
            "maintainer_contract": MAINTAINER_CONTRACT,
            "operation_schema": FILE_OPERATION_CONTRACT,
        }
        return bundle

    def _run_update_wiki_job(self, job: JobRecord, started_at: str) -> None:
        if not job.target_source_page_id:
            raise ValueError("update_wiki job is missing Target Source relation")
        if self.planner is None:
            raise ValueError("LLM planner is not configured")
        ensure_wiki_root(self.wiki_root)
        source = self.repository.get_source(job.target_source_page_id)
        artifacts_dir = self.wiki_root / "raw" / "sources" / source.source_id
        if not (artifacts_dir / "source.md").exists():
            raise ValueError(f"Missing source artefacts for {source.source_id}")
        bundle = self._build_llm_bundle(job, source, artifacts_dir)
        raw_model_output = self.planner.plan(bundle)
        self.repository.update_job_phase(job.page_id, "validating_plan")
        plan = parse_run_plan(raw_model_output)
        if plan.job_id != job.job_id:
            raise ValueError("Planner returned mismatched job_id")
        if plan.source_id != source.source_id:
            raise ValueError("Planner returned mismatched source_id")
        validate_run_plan(plan, root=self.wiki_root)
        state = apply_run_plan(plan, root=self.wiki_root)
        self.repository.update_job_phase(job.page_id, "applying_changes")
        changed = changed_files(plan, state, root=self.wiki_root)
        atomic_write_files(changed, root=self.wiki_root)
        source_page_path = f"wiki/sources/{source.source_id}.md"
        diff_path = write_diff(job.job_id, changed, root=self.wiki_root)
        manifest_path = update_manifest(
            root=self.wiki_root,
            source_id=source.source_id,
            checksum=json.loads((artifacts_dir / "metadata.json").read_text(encoding="utf-8"))["checksum"],
            source_page=source_page_path,
            affected_pages=plan.manifest_update["affected_pages"],
            job_id=job.job_id,
        )
        run_record_path = write_run_record(
            root=self.wiki_root,
            job_id=job.job_id,
            raw_model_output=raw_model_output,
            plan=plan,
            changed=changed,
            manifest_path=manifest_path,
        )
        self.repository.update_job_phase(job.page_id, "syncing_state")
        for relative_path in changed:
            if relative_path == "wiki/changelog/ingest-log.md":
                continue
            metadata = derive_wiki_page_metadata(relative_path, state[relative_path])
            self.repository.upsert_wiki_page(
                metadata,
                source_page_id=source.page_id,
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
