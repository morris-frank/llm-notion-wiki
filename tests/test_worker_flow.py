from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from llmwiki_runtime.llm import StaticPlanner
from llmwiki_runtime.models import JobRecord, ScopeContext, SourceArtifacts, SourceRecord, WikiPageMetadata
from llmwiki_runtime.paths import ScopedPaths
from llmwiki_runtime.worker import Worker
from llmwiki_runtime.wiki_ops import ensure_owner_scope, ensure_wiki_root


class FakeRepository:
    def __init__(self, source: SourceRecord) -> None:
        self.source = source
        self.created_jobs: list[tuple[str, str, str, str | None]] = []
        self.updated_source_ingest: dict[str, str] | None = None
        self.updated_source_summary: str | None = None
        self.succeeded_jobs: list[str] = []
        self.failed_jobs: list[tuple[str, str, str, str | None]] = []
        self.phases: list[str] = []
        self.upserted_pages: list[WikiPageMetadata] = []
        self.upserted_backing_source_page_ids: list[list[str]] = []
        self.claim_result: str | None = "2026-04-10T00:00:00Z"
        self.backing_source_error: Exception | None = None
        self.jobs_for_query: list[JobRecord] = []

    def get_source(self, source_page_id: str) -> SourceRecord:
        return self.source

    def active_policy_page_id(self, scope_context: ScopeContext | None = None) -> str:
        return "policy-page-id"

    def create_job(
        self,
        *,
        job_type: str,
        title: str,
        target_source_page_id: str,
        idempotency_key: str,
        scope_context: ScopeContext,
        policy_page_id: str | None = None,
    ) -> JobRecord:
        self.created_jobs.append((job_type, idempotency_key, scope_context.scope, scope_context.owner))
        return JobRecord(
            page_id=f"page-{job_type}",
            job_id=f"job-{job_type}",
            job_type=job_type,
            status="queued",
            queue_timestamp=None,
            scope=scope_context.scope,
            owner=scope_context.owner,
            target_source_page_id=target_source_page_id,
            idempotency_key=idempotency_key,
            policy_page_id=policy_page_id,
        )

    def claim_job(self, job: JobRecord, worker_name: str) -> str:
        return self.claim_result

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

    def resolve_backing_source_page_ids(self, source_ids: list[str], *, page_scope_context: ScopeContext) -> list[str]:
        if self.backing_source_error is not None:
            raise self.backing_source_error
        return [f"page-for-{source_id}" for source_id in source_ids]

    def upsert_wiki_page(self, metadata: WikiPageMetadata, *, backing_source_page_ids: list[str], latest_job_page_id: str) -> None:
        self.upserted_pages.append(metadata)
        self.upserted_backing_source_page_ids.append(backing_source_page_ids)

    def update_source_after_wiki(self, source: SourceRecord, *, source_summary_pointer: str) -> None:
        self.updated_source_summary = source_summary_pointer

    def mark_job_succeeded(self, page_id: str, *, started_at: str | None, output_pointer: str | None, diff_pointer: str | None) -> None:
        self.succeeded_jobs.append(page_id)

    def mark_job_failed(self, page_id: str, error_class: str, message: str, *, output_pointer: str | None = None) -> None:
        self.failed_jobs.append((page_id, error_class, message, output_pointer))

    def mark_source_failed(self, source: SourceRecord, message: str) -> None:
        self.failed_jobs.append((source.page_id, "source", message, None))

    def query_queued_jobs(self) -> list[JobRecord]:
        if not self.jobs_for_query:
            return []
        job = self.jobs_for_query[0]
        self.jobs_for_query = self.jobs_for_query[1:]
        return [job]

    def query_jobs(self, *, status: str | None = None, page_size: int = 20):
        if self.jobs_for_query:
            return list(self.jobs_for_query)
        jobs: list[JobRecord] = []
        for job_type, idempotency_key, scope, owner in self.created_jobs:
            jobs.append(
                JobRecord(
                    page_id=f"page-{job_type}",
                    job_id=f"job-{job_type}",
                    job_type=job_type,
                    status="queued",
                    queue_timestamp=None,
                    scope=scope,
                    owner=owner,
                    target_source_page_id=self.source.page_id,
                    idempotency_key=idempotency_key,
                )
            )
        return jobs


class FakeFetcher:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.error: Exception | None = None
        self.calls = 0

    def fetch(self, source: SourceRecord) -> SourceArtifacts:
        self.calls += 1
        if self.error is not None:
            raise self.error
        scoped_paths = ScopedPaths(self.root, source.scope_context)
        directory = scoped_paths.source_artifact_dir(source.source_id)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "source_id": source.source_id,
            "title": source.title,
            "scope": source.scope,
            "owner": source.owner,
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
    def test_private_ingest_then_update_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            ensure_owner_scope(root, "alice")
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/source",
                trust_level="primary",
                status="queued",
                scope="private",
                owner="alice",
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
                scope="private",
                owner="alice",
                target_source_page_id=source.page_id,
            )
            ingest_worker.run_job(ingest_job)
            self.assertTrue(repository.updated_source_ingest)
            self.assertTrue(any(job_type == "update_wiki" for job_type, _, _, _ in repository.created_jobs))

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
                    "wiki/users/alice/sources/src_1.md",
                    "wiki/users/alice/indexes/index.md",
                    "wiki/users/alice/indexes/ingest-log.md"
                  ],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "wiki/users/alice/sources/src_1.md",
                      "page_type": "source",
                      "reason": "Create the source summary page.",
                      "content": "---\\ntitle: \\"Example Source\\"\\npage_type: \\"source\\"\\nslug: \\"src-1\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nsource_scope:\\n  - \\"private\\"\\nentity_keys: []\\nconcept_keys:\\n  - \\"example-source\\"\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"private\\"\\nowner: \\"alice\\"\\nreview_state: \\"n_a\\"\\npromotion_origin: null\\nsource_type: \\"web_page\\"\\ncanonical_url: \\"https://example.com/source\\"\\nchecksum: \\"sha256:test\\"\\n---\\n# Example Source\\n\\n## One-line summary\\nA concise summary of the source.\\n\\n## Source summary\\nThis source introduces the example runtime. [S:src_1]\\n\\n## Main claims\\n- The worker can produce deterministic wiki updates. [S:src_1]\\n\\n## Important entities\\n- None.\\n\\n## Important concepts\\n- Example runtime [S:src_1]\\n\\n## Reliability notes\\n- This is a synthetic test source. [S:src_1]\\n\\n## Related pages\\n- [[index]]\\n\\n## Change log\\n- 2026-04-10: created from source src_1\\n\\n## Sources\\n- [S:src_1] Example Source. https://example.com/source\\n"
                    },
                    {
                      "op": "patch_sections",
                      "path": "wiki/users/alice/indexes/index.md",
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
                      "path": "wiki/users/alice/indexes/ingest-log.md",
                      "page_type": "changelog",
                      "reason": "Record the run.",
                      "content": "- 2026-04-10T00:00:00Z | job_update | src_1 | created wiki/users/alice/sources/src_1.md; updated wiki/users/alice/indexes/index.md"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/users/alice/sources/src_1.md",
                    "affected_pages": [
                      "wiki/users/alice/sources/src_1.md",
                      "wiki/users/alice/indexes/index.md",
                      "wiki/users/alice/indexes/ingest-log.md"
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
                scope="private",
                owner="alice",
                target_source_page_id=source.page_id,
            )
            update_worker.run_job(update_job)
            self.assertTrue((root / "wiki" / "users" / "alice" / "sources" / "src_1.md").exists())
            self.assertTrue((root / "state" / "manifests" / "users" / "alice" / "src_1.json").exists())
            self.assertTrue((root / "state" / "runs" / "users" / "alice" / "job_update.json").exists())
            self.assertTrue((root / "exports" / "diffs" / "users" / "alice" / "job_update.patch").exists())
            self.assertTrue(repository.upserted_pages)
            self.assertEqual(repository.upserted_backing_source_page_ids[0], ["page-for-src_1"])
            self.assertIsNotNone(repository.updated_source_summary)
            self.assertIn("update-page-id", repository.succeeded_jobs)
            self.assertTrue(all(page.scope == "private" and page.owner == "alice" for page in repository.upserted_pages))

    def test_private_bundle_includes_shared_overlay_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            ensure_owner_scope(root, "alice")
            shared_index = root / "wiki" / "shared" / "indexes" / "index.md"
            shared_index.write_text("shared index", encoding="utf-8")
            shared_manifest = root / "state" / "manifests" / "shared" / "shared_src.json"
            shared_manifest.write_text(
                json.dumps(
                    {
                        "source_id": "shared_src",
                        "scope": "shared",
                        "owner": None,
                        "checksum": "sha256:shared",
                        "source_page": "wiki/shared/sources/shared_src.md",
                        "affected_pages": ["wiki/shared/indexes/index.md"],
                        "last_job_id": "job_shared",
                        "last_updated_at": "2026-04-10T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (root / "wiki" / "shared" / "sources" / "shared_src.md").write_text("shared source", encoding="utf-8")
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/source",
                trust_level="primary",
                status="queued",
                scope="private",
                owner="alice",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=StaticPlanner(response="{}"), wiki_root=root, worker_name="test-worker")
            fetcher.fetch(source)
            bundle = worker._build_llm_bundle(
                JobRecord(
                    page_id="update-page-id",
                    job_id="job_update",
                    job_type="update_wiki",
                    status="queued",
                    queue_timestamp=None,
                    scope="private",
                    owner="alice",
                    target_source_page_id=source.page_id,
                ),
                source,
                ScopedPaths(root, source.scope_context),
                ScopedPaths(root, source.scope_context).source_artifact_dir(source.source_id),
            )
            self.assertIn("wiki/shared/indexes/index.md", bundle["existing_pages"])

    def test_invalid_planner_output_persists_failure_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            ensure_owner_scope(root, "alice")
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/source",
                trust_level="primary",
                status="queued",
                scope="private",
                owner="alice",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            worker = Worker(
                repository=repository,
                source_fetcher=fetcher,
                planner=StaticPlanner(response="not-json"),
                wiki_root=root,
                worker_name="test-worker",
            )
            update_job = JobRecord(
                page_id="update-page-id",
                job_id="job_update",
                job_type="update_wiki",
                status="queued",
                queue_timestamp=None,
                scope="private",
                owner="alice",
                target_source_page_id=source.page_id,
            )
            worker.run_job(update_job)
            record_path = root / "state" / "runs" / "users" / "alice" / "job_update.json"
            self.assertTrue(record_path.exists())
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["raw_model_output"], "not-json")
            self.assertEqual(payload["failure"]["error_class"], "validation")
            self.assertEqual(repository.failed_jobs[-1][0], "update-page-id")

    def test_planner_job_id_mismatch_persists_failure_run_record(self) -> None:
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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"wrong_job","source_id":"src_1","run_mode":"apply","summary":{"decision":"no_op","reason":"noop","review_required":false,"confidence":"medium"},"touched_paths":[],"operations":[{"op":"no_op","path":"wiki/shared/sources/src_1.md","page_type":"source","reason":"noop"}],"manifest_update":{"source_page":"wiki/shared/sources/src_1.md","affected_pages":[]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            worker.run_job(
                JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id)
            )
            record = json.loads((root / "state" / "runs" / "shared" / "job_update.json").read_text(encoding="utf-8"))
            self.assertIn("mismatched job_id", record["failure"]["message"])

    def test_planner_source_id_mismatch_persists_failure_run_record(self) -> None:
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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job_update","source_id":"wrong_src","run_mode":"apply","summary":{"decision":"no_op","reason":"noop","review_required":false,"confidence":"medium"},"touched_paths":[],"operations":[{"op":"no_op","path":"wiki/shared/sources/src_1.md","page_type":"source","reason":"noop"}],"manifest_update":{"source_page":"wiki/shared/sources/src_1.md","affected_pages":[]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            worker.run_job(
                JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id)
            )
            record = json.loads((root / "state" / "runs" / "shared" / "job_update.json").read_text(encoding="utf-8"))
            self.assertIn("mismatched source_id", record["failure"]["message"])

    def test_dry_run_writes_run_record_skips_disk_and_wiki_sync(self) -> None:
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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            body = """# Example Source

## One-line summary
Summary. [S:src_1]

## Source summary
Summary. [S:src_1]

## Main claims
- Claim. [S:src_1]

## Important entities
- None.

## Important concepts
- Concept [S:src_1]

## Reliability notes
- Note. [S:src_1]

## Related pages
- [[index]]

## Change log
- 2026-04-10: created

## Sources
- [S:src_1] Example Source. https://example.com/source
"""
            md_content = (
                "---\n"
                'title: "Example Source"\npage_type: source\nslug: src-1\nstatus: draft\n'
                'updated_at: "2026-04-10T00:00:00Z"\nsource_ids:\n  - "src_1"\nsource_scope:\n  - shared\n'
                "entity_keys: []\nconcept_keys: []\nconfidence: medium\nreview_required: false\n"
                'scope: shared\nowner: null\nreview_state: unreviewed\npromotion_origin: null\n'
                'source_type: web_page\ncanonical_url: "https://example.com/source"\nchecksum: "sha256:test"\n---\n'
                + body
            )
            plan_payload = {
                "schema_version": "v1",
                "job_id": "job_update",
                "source_id": "src_1",
                "run_mode": "dry_run",
                "summary": {
                    "decision": "create_new_pages",
                    "reason": "Dry run only.",
                    "review_required": False,
                    "confidence": "medium",
                },
                "touched_paths": ["wiki/shared/sources/src_1.md"],
                "operations": [
                    {
                        "op": "create_file",
                        "path": "wiki/shared/sources/src_1.md",
                        "page_type": "source",
                        "reason": "Create source summary.",
                        "content": md_content,
                    }
                ],
                "manifest_update": {
                    "source_page": "wiki/shared/sources/src_1.md",
                    "affected_pages": ["wiki/shared/sources/src_1.md"],
                },
                "warnings": [],
            }
            planner = StaticPlanner(response=json.dumps(plan_payload))
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            worker.run_job(
                JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id)
            )
            record = json.loads((root / "state" / "runs" / "shared" / "job_update.json").read_text(encoding="utf-8"))
            self.assertTrue(record.get("dry_run"))
            self.assertIsNone(record.get("failure"))
            self.assertEqual(repository.upserted_pages, [])
            self.assertIsNone(repository.updated_source_summary)
            self.assertFalse((root / "wiki" / "shared" / "sources" / "src_1.md").exists())
            diff_path = root / "exports" / "diffs" / "shared" / "job_update.patch"
            self.assertFalse(diff_path.exists())
            self.assertFalse((root / "state" / "manifests" / "shared" / "src_1.json").exists())

    def test_lost_job_claim_skips_execution(self) -> None:
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
                scope="shared",
            )
            repository = FakeRepository(source)
            repository.claim_result = None
            worker = Worker(
                repository=repository,
                source_fetcher=FakeFetcher(root),
                planner=None,
                wiki_root=root,
                worker_name="test-worker",
            )
            worker.run_job(
                JobRecord(
                    page_id="ingest-page-id",
                    job_id="job_ingest",
                    job_type="ingest_source",
                    status="queued",
                    queue_timestamp=None,
                    scope="shared",
                    owner=None,
                    target_source_page_id=source.page_id,
                )
            )
            self.assertIsNone(repository.updated_source_ingest)
            self.assertFalse(repository.failed_jobs)
            self.assertFalse(repository.succeeded_jobs)

    def test_missing_source_artifacts_fail_before_apply(self) -> None:
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
                scope="shared",
            )
            repository = FakeRepository(source)
            worker = Worker(repository=repository, source_fetcher=FakeFetcher(root), planner=StaticPlanner(response="{}"), wiki_root=root, worker_name="test-worker")
            worker.run_job(JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id))
            self.assertEqual(repository.failed_jobs[-1][1], "validation")
            self.assertIn("Missing source artefacts", repository.failed_jobs[-1][2])

    def test_source_job_scope_mismatch_fails(self) -> None:
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
                scope="private",
                owner="alice",
            )
            repository = FakeRepository(source)
            worker = Worker(repository=repository, source_fetcher=FakeFetcher(root), planner=None, wiki_root=root, worker_name="test-worker")
            worker.run_job(JobRecord("ingest-page-id", "job_ingest", "ingest_source", "queued", None, "shared", None, source.page_id))
            self.assertIn("Source scope/owner does not match", repository.failed_jobs[-1][2])

    def test_backing_source_resolution_error_fails_update_cleanly(self) -> None:
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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            repository.backing_source_error = ValueError("ambiguous source")
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job_update","source_id":"src_1","run_mode":"apply","summary":{"decision":"mixed","reason":"create","review_required":false,"confidence":"medium"},"touched_paths":["wiki/shared/sources/src_1.md"],"operations":[{"op":"create_file","path":"wiki/shared/sources/src_1.md","page_type":"source","reason":"create","content":"---\\ntitle: \\"Source\\"\\npage_type: \\"source\\"\\nslug: \\"src-1\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nsource_scope:\\n  - \\"shared\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: null\\n---\\n# Source\\n\\n## One-line summary\\nSummary\\n\\n## Source summary\\nBody [S:src_1]\\n\\n## Main claims\\n- Claim [S:src_1]\\n\\n## Important entities\\n\\n## Important concepts\\n\\n## Reliability notes\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_1] Example"}],"manifest_update":{"source_page":"wiki/shared/sources/src_1.md","affected_pages":["wiki/shared/sources/src_1.md"]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            worker.run_job(JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id))
            self.assertEqual(repository.failed_jobs[-1][1], "validation")
            self.assertIn("ambiguous source", repository.failed_jobs[-1][2])

    def test_ingest_fetch_error_marks_source_and_job_failed(self) -> None:
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
                scope="shared",
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.error = OSError("network down")
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=None, wiki_root=root, worker_name="test-worker")
            worker.run_job(JobRecord("ingest-page-id", "job_ingest", "ingest_source", "queued", None, "shared", None, source.page_id))
            self.assertEqual(repository.failed_jobs[0][0], "source-page-id")
            self.assertEqual(repository.failed_jobs[-1][0], "ingest-page-id")
            self.assertEqual(repository.failed_jobs[-1][1], "external_io")

    def test_later_run_no_op_succeeds_when_canonical_source_page_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            source_page = root / "wiki" / "shared" / "sources" / "src_1.md"
            source_page.parent.mkdir(parents=True, exist_ok=True)
            source_page.write_text(
                "---\n"
                'title: "Existing"\n'
                'page_type: "source"\n'
                'slug: "src-1"\n'
                'status: "draft"\n'
                'updated_at: "2026-04-10T00:00:00Z"\n'
                "source_ids:\n"
                '  - "src_1"\n'
                "source_scope:\n"
                '  - "shared"\n'
                "entity_keys: []\n"
                "concept_keys: []\n"
                'confidence: "medium"\n'
                "review_required: false\n"
                'scope: "shared"\n'
                "owner: null\n"
                'review_state: "unreviewed"\n'
                "promotion_origin: null\n"
                "---\n"
                "# Existing\n\n## One-line summary\nSummary\n\n## Source summary\nBody [S:src_1]\n\n## Main claims\n- Claim [S:src_1]\n\n## Important entities\n\n## Important concepts\n\n## Reliability notes\n\n## Related pages\n\n## Change log\n- created\n\n## Sources\n- [S:src_1] Example\n",
                encoding="utf-8",
            )
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/source",
                trust_level="primary",
                status="queued",
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job_update","source_id":"src_1","run_mode":"apply","summary":{"decision":"no_op","reason":"Nothing changed.","review_required":false,"confidence":"medium"},"touched_paths":[],"operations":[{"op":"no_op","path":"wiki/shared/sources/src_1.md","page_type":"source","reason":"noop"}],"manifest_update":{"source_page":"wiki/shared/sources/src_1.md","affected_pages":[]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            worker.run_job(JobRecord("update-page-id", "job_update", "update_wiki", "queued", None, "shared", None, source.page_id))
            self.assertIn("update-page-id", repository.succeeded_jobs)
            self.assertFalse(repository.failed_jobs)

    def test_service_app_smoke_with_worker_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from llmwiki_runtime.config import Settings
            from llmwiki_runtime.service import ServiceApp

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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job_update_wiki","source_id":"src_1","run_mode":"apply","summary":{"decision":"mixed","reason":"create source page","review_required":false,"confidence":"medium"},"touched_paths":["wiki/shared/sources/src_1.md"],"operations":[{"op":"create_file","path":"wiki/shared/sources/src_1.md","page_type":"source","reason":"create","content":"---\\ntitle: \\"Example Source\\"\\npage_type: \\"source\\"\\nslug: \\"src-1\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nsource_scope:\\n  - \\"shared\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: null\\n---\\n# Example Source\\n\\n## One-line summary\\nSummary\\n\\n## Source summary\\nBody [S:src_1]\\n\\n## Main claims\\n- Claim [S:src_1]\\n\\n## Important entities\\n\\n## Important concepts\\n\\n## Reliability notes\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_1] Example"}],"manifest_update":{"source_page":"wiki/shared/sources/src_1.md","affected_pages":["wiki/shared/sources/src_1.md"]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=fetcher, planner=planner, wiki_root=root, worker_name="test-worker")
            settings = Settings(
                notion_token="token",
                notion_version="2026-03-11",
                notion_api_base="https://api.notion.com/v1",
                control_db_id=None,
                sources_data_source_id="sources",
                wiki_data_source_id="wiki",
                jobs_data_source_id="jobs",
                policies_data_source_id="policies",
                wiki_root=root,
                worker_name="test-worker",
                poll_interval_seconds=5,
                admin_api_key=None,
                llm_api_key=None,
                llm_api_base="https://example.com/v1",
                llm_model=None,
                notion_webhook_signing_secret=None,
                notion_webhook_verification_token=None,
                log_level="INFO",
            )
            app = ServiceApp(settings=settings, worker=worker)
            enqueue_payload = app.enqueue_source(source.page_id)
            ingest_page_id = "page-ingest_source"
            repository.jobs_for_query = [
                JobRecord(
                    page_id=ingest_page_id,
                    job_id="job_ingest_source",
                    job_type="ingest_source",
                    status="queued",
                    queue_timestamp=None,
                    scope="shared",
                    owner=None,
                    target_source_page_id=source.page_id,
                ),
                JobRecord(
                    page_id="page-update_wiki",
                    job_id="job_update_wiki",
                    job_type="update_wiki",
                    status="queued",
                    queue_timestamp=None,
                    scope="shared",
                    owner=None,
                    target_source_page_id=source.page_id,
                ),
            ]
            first = worker.run_once()
            second = worker.run_once()
            inspect_payload = app.inspect_jobs(None)
            self.assertEqual(enqueue_payload["job_id"], "job-ingest_source")
            self.assertEqual(first.job_type, "ingest_source")
            self.assertEqual(second.job_type, "update_wiki")
            self.assertTrue((root / "wiki" / "shared" / "sources" / "src_1.md").exists())
            self.assertTrue(inspect_payload["jobs"])

    def test_first_run_no_op_fails_without_canonical_source_page(self) -> None:
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
                scope="shared",
                content_version=1,
            )
            repository = FakeRepository(source)
            fetcher = FakeFetcher(root)
            fetcher.fetch(source)
            planner = StaticPlanner(
                response="""
                {
                  "schema_version": "v1",
                  "job_id": "job_update",
                  "source_id": "src_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "no_op",
                    "reason": "Nothing changed.",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": [],
                  "operations": [
                    {
                      "op": "no_op",
                      "path": "wiki/shared/sources/src_1.md",
                      "page_type": "source",
                      "reason": "No change required."
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/shared/sources/src_1.md",
                    "affected_pages": []
                  },
                  "warnings": []
                }
                """
            )
            worker = Worker(
                repository=repository,
                source_fetcher=fetcher,
                planner=planner,
                wiki_root=root,
                worker_name="test-worker",
            )
            worker.run_job(
                JobRecord(
                    page_id="update-page-id",
                    job_id="job_update",
                    job_type="update_wiki",
                    status="queued",
                    queue_timestamp=None,
                    scope="shared",
                    owner=None,
                    target_source_page_id=source.page_id,
                )
            )
            self.assertEqual(repository.failed_jobs[-1][0], "update-page-id")
            self.assertIsNone(repository.updated_source_summary)


if __name__ == "__main__":
    unittest.main()
