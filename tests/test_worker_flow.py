from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from llmwiki_runtime.llm import StaticPlanner
from llmwiki_runtime.models import JobRecord, SourceArtifacts, SourceRecord, WikiPageMetadata
from llmwiki_runtime.worker import Worker
from llmwiki_runtime.wiki_ops import ensure_wiki_root


class FakeRepository:
    def __init__(self, source: SourceRecord) -> None:
        self.source = source
        self.created_jobs: list[tuple[str, str]] = []
        self.updated_source_ingest: dict[str, str] | None = None
        self.updated_source_summary: str | None = None
        self.succeeded_jobs: list[str] = []
        self.failed_jobs: list[tuple[str, str, str]] = []
        self.phases: list[str] = []
        self.upserted_pages: list[WikiPageMetadata] = []

    def get_source(self, source_page_id: str) -> SourceRecord:
        return self.source

    def active_policy_page_id(self) -> str:
        return "policy-page-id"

    def create_job(self, *, job_type: str, title: str, target_source_page_id: str, idempotency_key: str, policy_page_id: str | None = None) -> JobRecord:
        self.created_jobs.append((job_type, idempotency_key))
        return JobRecord(
            page_id=f"page-{job_type}",
            job_id=f"job-{job_type}",
            job_type=job_type,
            status="queued",
            queue_timestamp=None,
            target_source_page_id=target_source_page_id,
            idempotency_key=idempotency_key,
            policy_page_id=policy_page_id,
        )

    def claim_job(self, job: JobRecord, worker_name: str) -> str:
        return "2026-04-10T00:00:00Z"

    def mark_source_fetching(self, source: SourceRecord) -> None:
        return

    def update_source_for_ingest(self, source: SourceRecord, *, checksum: str, raw_text_pointer: str, markdown_pointer: str) -> None:
        self.updated_source_ingest = {
            "checksum": checksum,
            "raw_text_pointer": raw_text_pointer,
            "markdown_pointer": markdown_pointer,
        }
        self.source.checksum = checksum

    def update_job_phase(self, page_id: str, phase: str) -> None:
        self.phases.append(phase)

    def upsert_wiki_page(self, metadata: WikiPageMetadata, *, source_page_id: str, latest_job_page_id: str) -> None:
        self.upserted_pages.append(metadata)

    def update_source_after_wiki(self, source: SourceRecord, *, source_summary_pointer: str) -> None:
        self.updated_source_summary = source_summary_pointer

    def mark_job_succeeded(self, page_id: str, *, started_at: str | None, output_pointer: str | None, diff_pointer: str | None) -> None:
        self.succeeded_jobs.append(page_id)

    def mark_job_failed(self, page_id: str, error_class: str, message: str) -> None:
        self.failed_jobs.append((page_id, error_class, message))

    def mark_source_failed(self, source: SourceRecord, message: str) -> None:
        self.failed_jobs.append((source.page_id, "source", message))

    def query_queued_jobs(self) -> list[JobRecord]:
        return []


class FakeFetcher:
    def __init__(self, root: Path) -> None:
        self.root = root

    def fetch(self, source: SourceRecord) -> SourceArtifacts:
        directory = self.root / "raw" / "sources" / source.source_id
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "source_id": source.source_id,
            "title": source.title,
            "checksum": "sha256:test",
        }
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / "source.txt").write_text("plain text", encoding="utf-8")
        (directory / "source.md").write_text("# Source\n\nEvidence paragraph.\n", encoding="utf-8")
        return SourceArtifacts(
            metadata=metadata,
            raw_text="plain text",
            markdown="# Source\n\nEvidence paragraph.\n",
            checksum="sha256:test",
            storage_dir=directory,
        )


class WorkerFlowTests(unittest.TestCase):
    def test_ingest_then_update_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/source",
                trust_level="primary",
                status="queued",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            ingest_worker = Worker(repository=repository, source_fetcher=fetcher, planner=None, wiki_root=root, worker_name="test-worker")
            ingest_job = JobRecord(
                page_id="ingest-page-id",
                job_id="job_ingest",
                job_type="ingest_source",
                status="queued",
                queue_timestamp=None,
                target_source_page_id=source.page_id,
            )
            ingest_worker.run_job(ingest_job)
            self.assertTrue(repository.updated_source_ingest)
            self.assertTrue(any(job_type == "update_wiki" for job_type, _ in repository.created_jobs))

            planner = StaticPlanner(
                response="""
                {
                  "schema_version": "v1",
                  "job_id": "job_update",
                  "source_id": "src_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "mixed",
                    "reason": "Create a source page and update navigation.",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": [
                    "wiki/sources/src_1.md",
                    "wiki/index.md",
                    "wiki/changelog/ingest-log.md"
                  ],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "wiki/sources/src_1.md",
                      "page_type": "source",
                      "reason": "Create the source summary page.",
                      "content": "---\\ntitle: \\"Example Source\\"\\npage_type: \\"source\\"\\nslug: \\"src-1\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nentity_keys: []\\nconcept_keys:\\n  - \\"example-source\\"\\nconfidence: \\"medium\\"\\nreview_required: false\\nsource_type: \\"web_page\\"\\ncanonical_url: \\"https://example.com/source\\"\\nchecksum: \\"sha256:test\\"\\n---\\n# Example Source\\n\\n## One-line summary\\nA concise summary of the source.\\n\\n## Source summary\\nThis source introduces the example runtime. [S:src_1]\\n\\n## Main claims\\n- The worker can produce deterministic wiki updates. [S:src_1]\\n\\n## Important entities\\n- None.\\n\\n## Important concepts\\n- Example runtime [S:src_1]\\n\\n## Reliability notes\\n- This is a synthetic test source. [S:src_1]\\n\\n## Related pages\\n- [[index]]\\n\\n## Change log\\n- 2026-04-10: created from source src_1\\n\\n## Sources\\n- [S:src_1] Example Source. https://example.com/source\\n"
                    },
                    {
                      "op": "patch_sections",
                      "path": "wiki/index.md",
                      "page_type": "index",
                      "reason": "Add the new source to the index.",
                      "section_patches": [
                        {
                          "section": "## Related pages",
                          "action": "append",
                          "content": "- [[src-1]]"
                        },
                        {
                          "section": "## Sources",
                          "action": "append",
                          "content": "- [S:src_1] Example Source. https://example.com/source"
                        },
                        {
                          "section": "## Change log",
                          "action": "append",
                          "content": "- 2026-04-10: updated with source src_1"
                        }
                      ]
                    },
                    {
                      "op": "append_block",
                      "path": "wiki/changelog/ingest-log.md",
                      "page_type": "changelog",
                      "reason": "Record the run.",
                      "content": "- 2026-04-10T00:00:00Z | job_update | src_1 | created wiki/sources/src_1.md; updated wiki/index.md"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/sources/src_1.md",
                    "affected_pages": [
                      "wiki/sources/src_1.md",
                      "wiki/index.md",
                      "wiki/changelog/ingest-log.md"
                    ]
                  },
                  "warnings": []
                }
                """
            )
            update_worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            update_job = JobRecord(
                page_id="update-page-id",
                job_id="job_update",
                job_type="update_wiki",
                status="queued",
                queue_timestamp=None,
                target_source_page_id=source.page_id,
            )
            update_worker.run_job(update_job)
            self.assertTrue((root / "wiki" / "sources" / "src_1.md").exists())
            self.assertTrue((root / "state" / "manifests" / "src_1.json").exists())
            self.assertTrue((root / "state" / "runs" / "job_update.json").exists())
            self.assertTrue((root / "exports" / "diffs" / "job_update.patch").exists())
            self.assertTrue(repository.upserted_pages)
            self.assertIsNotNone(repository.updated_source_summary)
            self.assertIn("update-page-id", repository.succeeded_jobs)


if __name__ == "__main__":
    unittest.main()
