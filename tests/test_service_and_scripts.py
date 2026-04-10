from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest

from llmwiki_runtime.config import Settings
from llmwiki_runtime.models import ScopeContext
from llmwiki_runtime.service import ServiceApp


class StubRepository:
    def __init__(self) -> None:
        self.created_jobs: list[tuple[str, str, ScopeContext]] = []
        self.source = type(
            "Source",
            (),
            {
                "page_id": "source-page-id",
                "source_id": "src_1",
                "title": "Source",
                "scope": "private",
                "owner": "alice",
                "scope_context": ScopeContext("private", "alice"),
                "trigger_regeneration": False,
                "content_version": 3,
                "checksum": "sha256:test",
                "last_edited_time": "2026-04-10T00:00:00Z",
                "properties": {"Source Title": {}},
            },
        )()

    def get_source(self, source_page_id: str):
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
    ):
        self.created_jobs.append((job_type, idempotency_key, scope_context))
        return type("Job", (), {"job_id": "job-1", "scope": scope_context.scope, "owner": scope_context.owner})()

    def query_jobs(self, *, status: str | None = None, page_size: int = 20):
        return []

    def requeue_job(self, job_page_id: str):
        return type("Job", (), {"job_id": "job-2", "page_id": job_page_id, "status": "queued", "scope": "private", "owner": "alice"})()


class StubWorker:
    def __init__(self) -> None:
        self.repository = StubRepository()

    def enqueue_ingest_job(self, source_page_id: str):
        return type(
            "Job",
            (),
            {
                "job_id": "job-1",
                "page_id": "page-1",
                "status": "queued",
                "target_source_page_id": source_page_id,
                "scope": "private",
                "owner": "alice",
            },
        )()


class ServiceAndScriptTests(unittest.TestCase):
    def test_webhook_signature_and_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                notion_token="token",
                notion_version="2026-03-11",
                notion_api_base="https://api.notion.com/v1",
                control_db_id=None,
                sources_data_source_id="sources",
                wiki_data_source_id="wiki",
                jobs_data_source_id="jobs",
                policies_data_source_id="policies",
                wiki_root=Path(tmpdir),
                worker_name="worker",
                poll_interval_seconds=5,
                admin_api_key=None,
                llm_api_key=None,
                llm_api_base="https://example.com/v1",
                llm_model=None,
                notion_webhook_signing_secret="signing-secret",
                notion_webhook_verification_token="verify-token",
                log_level="INFO",
            )
            app = ServiceApp(settings=settings, worker=StubWorker())
            body = b'{"type":"page.properties_updated","entity":{"id":"source-page-id","type":"page"}}'
            signature = hmac.new(b"signing-secret", body, hashlib.sha256).hexdigest()
            status, payload = app.handle_webhook(body, {"X-Notion-Signature": f"sha256={signature}"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["accepted"])
            self.assertTrue(app.worker.repository.created_jobs)
            _, key, scope_context = app.worker.repository.created_jobs[0]
            self.assertEqual(scope_context.scope, "private")
            self.assertEqual(scope_context.owner, "alice")
            self.assertIn(":private:alice:", key)

    def test_scripts_include_scope_contract(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        bootstrap = (repo_root / "bootstrap_llmwiki_notion_dynamic.sh").read_text(encoding="utf-8")
        verify = (repo_root / "verify_llmwiki_notion_dynamic.sh").read_text(encoding="utf-8")
        setup = (repo_root / "llmwiki_notion_setup.sh").read_text(encoding="utf-8")
        self.assertIn("Confidence Level", bootstrap)
        self.assertIn("Job Phase", bootstrap)
        self.assertIn("Scope", bootstrap)
        self.assertIn("Owner", bootstrap)
        self.assertIn("Review State", bootstrap)
        self.assertIn("Policy Target Scope", bootstrap)
        self.assertIn("Scope", verify)
        self.assertIn("Owner", verify)
        self.assertIn("Enable entities data source?\" 0", setup)
        self.assertIn("Enable source enrichment?\" 1", setup)


if __name__ == "__main__":
    unittest.main()
