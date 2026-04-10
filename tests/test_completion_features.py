from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from llmwiki_runtime.config import Settings
from llmwiki_runtime.llm import StaticPlanner
from llmwiki_runtime.models import (
    JobRecord,
    PolicyRecord,
    PromotionRecord,
    QuestionRecord,
    ScopeContext,
    WebhookResolvePromotion,
    WebhookResolveQuestion,
    WikiPageMetadata,
)
from llmwiki_runtime.repository import NotionRepository
from llmwiki_runtime.service import ServiceApp
from llmwiki_runtime.worker import Worker
from llmwiki_runtime.wiki_ops import ensure_owner_scope, ensure_wiki_root


def _rich(value: str | None) -> list[dict]:
    if value is None:
        return []
    return [{"plain_text": value}]


def _title(value: str) -> list[dict]:
    return [{"plain_text": value}]


class PolicyClient:
    def __init__(self) -> None:
        self.policy_rows: list[dict] = []
        self.pages: dict[str, dict] = {}

    def query_data_source(self, *args, **kwargs):
        return {"results": list(self.policy_rows)}

    def retrieve_page(self, page_id: str) -> dict:
        return self.pages[page_id]

    def page_markdown(self, page_id: str, *, title: str | None = None) -> str:
        return f"# {title or 'Policy'}\n\nPolicy body for {page_id}\n"


class CompletionRepositoryTests(unittest.TestCase):
    def test_load_effective_policy_prefers_highest_priority_compatible_policy(self) -> None:
        client = PolicyClient()
        client.policy_rows = [
            {
                "id": "policy-global",
                "properties": {
                    "Policy Name": {"title": _title("Global")},
                    "Active": {"checkbox": True},
                    "Policy Target Scope": {"select": {"name": "all"}},
                    "Policy Owner": {"rich_text": []},
                    "Policy Priority": {"number": 10},
                    "Allowed Page Types": {"multi_select": [{"name": "source"}]},
                    "Question Mode": {"select": {"name": "mixed"}},
                    "Entity Extraction": {"select": {"name": "minimal"}},
                    "Promotion Required For Shared": {"checkbox": True},
                    "Minimum Review State For Shared": {"select": {"name": "in_review"}},
                    "Requires Human Review": {"checkbox": True},
                    "Auto Publish Allowed": {"checkbox": False},
                },
            },
            {
                "id": "policy-private-alice",
                "properties": {
                    "Policy Name": {"title": _title("Alice Private")},
                    "Active": {"checkbox": True},
                    "Policy Target Scope": {"select": {"name": "private"}},
                    "Policy Owner": {"rich_text": _rich("alice")},
                    "Policy Priority": {"number": 50},
                    "Allowed Page Types": {"multi_select": [{"name": "faq"}]},
                    "Question Mode": {"select": {"name": "faq"}},
                    "Entity Extraction": {"select": {"name": "off"}},
                    "Promotion Required For Shared": {"checkbox": True},
                    "Minimum Review State For Shared": {"select": {"name": "approved"}},
                    "Requires Human Review": {"checkbox": True},
                    "Auto Publish Allowed": {"checkbox": False},
                },
            },
        ]
        client.pages = {
            "policy-global": client.policy_rows[0],
            "policy-private-alice": client.policy_rows[1],
        }
        repository = NotionRepository(client, "sources", "wiki", "jobs", "policies")
        policy = repository.load_effective_policy(ScopeContext("private", "alice"))
        self.assertEqual(policy.page_id, "policy-private-alice")
        self.assertEqual(policy.question_mode, "faq")
        self.assertIn("Policy body", policy.content_markdown)


class ServiceRoutingRepository:
    def __init__(self) -> None:
        self.created_jobs: list[tuple[str, str]] = []
        self.questions_data_source_id = "questions"
        self.promotions_data_source_id = "promotions"
        self.client = self

    def retrieve_page(self, page_id: str) -> dict:
        if page_id == "question-page":
            return {
                "id": page_id,
                "properties": {
                    "Question": {"title": _title("Question text")},
                    "Question ID": {"rich_text": _rich("q_1")},
                    "Question Status": {"select": {"name": "queued"}},
                    "Scope": {"select": {"name": "shared"}},
                    "Owner": {"rich_text": []},
                },
            }
        return {
            "id": page_id,
            "properties": {
                "Promotion ID": {"rich_text": _rich("promo_1")},
                "Status": {"select": {"name": "approved"}},
                "Scope": {"select": {"name": "private"}},
                "Owner": {"rich_text": _rich("alice")},
                "Decision": {"rich_text": _rich("Approved")},
                "Submitted By": {"rich_text": _rich("alice")},
                "Reviewed By": {"rich_text": _rich("reviewer")},
                "Source Private Page": {"relation": [{"id": "wiki-private"}]},
                "Target Shared Pages": {"relation": []},
                "Latest Job": {"relation": []},
            },
        }

    def _question_from_page(self, page: dict) -> QuestionRecord:
        return QuestionRecord(page_id=page["id"], question_id="q_1", question="Question text", status="queued", scope="shared")

    def _promotion_from_page(self, page: dict) -> PromotionRecord:
        return PromotionRecord(
            page_id=page["id"],
            promotion_id="promo_1",
            scope="private",
            owner="alice",
            status="approved",
            decision="Approved",
            submitted_by="alice",
            reviewed_by="reviewer",
            source_private_page_id="wiki-private",
            target_shared_page_ids=[],
            latest_job_page_id=None,
        )

    def active_policy_page_id(self, scope_context: ScopeContext | None = None) -> str:
        return "policy-page-id"

    def create_job(self, **kwargs):
        self.created_jobs.append((kwargs["job_type"], kwargs["idempotency_key"]))
        return type("Job", (), {"job_id": "job-created", "scope": kwargs["scope_context"].scope, "owner": kwargs["scope_context"].owner})()

    def resolve_webhook_page(self, page_id: str) -> WebhookResolveQuestion | WebhookResolvePromotion | None:
        page = self.retrieve_page(page_id)
        properties = page.get("properties", {})
        if "Question" in properties and self.questions_data_source_id:
            return WebhookResolveQuestion(question=self._question_from_page(page))
        if "Promotion ID" in properties and self.promotions_data_source_id:
            return WebhookResolvePromotion(promotion=self._promotion_from_page(page))
        return None

    def query_jobs(self, *, status: str | None = None, page_size: int = 20):
        return []

    def requeue_job(self, job_page_id: str):
        return JobRecord(page_id=job_page_id, job_id="job-1", job_type="ingest_source", status="queued", queue_timestamp=None, scope="shared", owner=None)


class ServiceRoutingTests(unittest.TestCase):
    def _app(self, tmpdir: str) -> ServiceApp:
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
            questions_data_source_id="questions",
            promotions_data_source_id="promotions",
            public_base_url="https://example.ngrok.app",
        )
        worker = type("Worker", (), {"repository": ServiceRoutingRepository(), "enqueue_ingest_job": lambda self, source_page_id: None})()
        return ServiceApp(settings=settings, worker=worker)

    def _signed_headers(self, payload: dict) -> dict[str, str]:
        body = json.dumps(payload).encode("utf-8")
        digest = hmac.new(b"signing-secret", body, hashlib.sha256).hexdigest()
        return {"X-Notion-Signature": f"sha256={digest}"}

    def test_webhook_routes_question_rows_to_answer_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app(tmpdir)
            payload = {"type": "page.created", "timestamp": "2026-04-10T00:00:00Z", "entity": {"id": "question-page", "type": "page"}}
            status, response = app.handle_webhook(json.dumps(payload).encode("utf-8"), self._signed_headers(payload))
            self.assertEqual(status, 200)
            self.assertTrue(response["accepted"])
            self.assertEqual(app.worker.repository.created_jobs[-1][0], "answer_question")

    def test_webhook_routes_approved_promotions_to_promote_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app(tmpdir)
            payload = {"type": "page.updated", "timestamp": "2026-04-10T00:00:00Z", "entity": {"id": "promotion-page", "type": "page"}}
            status, response = app.handle_webhook(json.dumps(payload).encode("utf-8"), self._signed_headers(payload))
            self.assertEqual(status, 200)
            self.assertTrue(response["accepted"])
            self.assertEqual(app.worker.repository.created_jobs[-1][0], "promote_private")

    def test_webhook_status_exposes_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app(tmpdir)
            status = app.webhook_status()
            self.assertTrue(status["ready"])
            self.assertEqual(status["endpoint"], "https://example.ngrok.app/notion/webhook")


class NewWorkerRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.question = QuestionRecord(page_id="question-page", question_id="q_1", question="What is the answer?", status="queued", scope="shared")
        self.promotion = PromotionRecord(
            page_id="promotion-page",
            promotion_id="promo_1",
            scope="private",
            owner="alice",
            status="approved",
            decision="Approved",
            submitted_by="alice",
            reviewed_by="reviewer",
            source_private_page_id="wiki-private-page",
            target_shared_page_ids=[],
            latest_job_page_id=None,
        )
        self.succeeded_jobs: list[str] = []
        self.question_updates: list[tuple[str, str | None]] = []
        self.promotion_updates: list[str] = []
        self.upserted_pages: list[WikiPageMetadata] = []

    def claim_job(self, job: JobRecord, worker_name: str) -> str:
        return "2026-04-10T00:00:00Z"

    def mark_job_succeeded(self, page_id: str, *, started_at: str | None, output_pointer: str | None, diff_pointer: str | None) -> None:
        self.succeeded_jobs.append(page_id)

    def mark_job_failed(self, page_id: str, error_class: str, message: str, *, output_pointer: str | None = None) -> None:
        raise AssertionError(f"unexpected failure {error_class}: {message}")

    def update_job_phase(self, page_id: str, phase: str) -> None:
        return

    def load_effective_policy(self, scope_context: ScopeContext) -> PolicyRecord | None:
        return PolicyRecord(
            page_id="policy",
            name="Default",
            version="v1",
            target_scope="all",
            owner=None,
            priority=1,
            active=True,
            allowed_page_types=["faq", "question", "concept"],
            question_mode="mixed",
            entity_extraction="minimal",
            promotion_required_for_shared=True,
            minimum_review_state_for_shared="in_review",
            requires_human_review=True,
            auto_publish_allowed=False,
            max_source_count=None,
            prompt_bundle_pointer=None,
            citation_policy_pointer=None,
            page_template_pointer=None,
            content_markdown="# Policy\n",
        )

    def get_question(self, question_page_id: str) -> QuestionRecord:
        return self.question

    def resolve_backing_source_page_ids(self, source_ids: list[str], *, page_scope_context: ScopeContext) -> list[str]:
        return []

    def upsert_wiki_page(self, metadata: WikiPageMetadata, *, backing_source_page_ids: list[str], latest_job_page_id: str, related_entity_page_ids: list[str] | None = None) -> str:
        self.upserted_pages.append(metadata)
        return f"wiki-row-{metadata.slug}"

    def update_question_after_answer(
        self,
        question: QuestionRecord,
        *,
        latest_job_page_id: str,
        target_wiki_page_id: str | None,
        answer_page_slug: str | None,
        resolution_type: str,
    ) -> None:
        self.question_updates.append((resolution_type, answer_page_slug))

    def get_promotion(self, promotion_page_id: str) -> PromotionRecord:
        return self.promotion

    def get_wiki_page(self, wiki_page_id: str) -> WikiPageMetadata:
        return WikiPageMetadata(
            path="wiki/users/alice/concepts/private-note.md",
            title="Private Note",
            slug="private-note",
            page_type="concept",
            status="draft",
            confidence="medium",
            review_required=False,
            source_ids=[],
            source_scope=[],
            scope="private",
            owner="alice",
            review_state="n_a",
            promotion_origin=None,
            summary="Private summary",
        )

    def update_promotion_after_apply(self, promotion: PromotionRecord, *, latest_job_page_id: str) -> None:
        self.promotion_updates.append(latest_job_page_id)


class NewWorkerTests(unittest.TestCase):
    def test_answer_question_creates_faq_and_updates_question_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            repository = NewWorkerRepository(root)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job-answer","source_id":"q_1","run_mode":"apply","summary":{"decision":"mixed","reason":"answer question","review_required":true,"confidence":"medium"},"touched_paths":["wiki/shared/faq/answer.md"],"operations":[{"op":"create_file","path":"wiki/shared/faq/answer.md","page_type":"faq","reason":"answer","content":"---\\ntitle: \\"Answer\\"\\npage_type: \\"faq\\"\\nslug: \\"answer\\"\\nstatus: \\"published\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids: []\\nsource_scope: []\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: null\\n---\\n# Answer\\n\\n## One-line summary\\nAnswer summary.\\n\\n## Key points\\n- Point one.\\n\\n## Details\\nDetails.\\n\\n## Evidence\\n- Derived from existing wiki knowledge.\\n\\n## Open questions\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n"}],"manifest_update":{"source_page":"wiki/shared/faq/answer.md","affected_pages":["wiki/shared/faq/answer.md"]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=None, planner=planner, wiki_root=root, worker_name="worker")  # type: ignore[arg-type]
            job = JobRecord(page_id="job-page", job_id="job-answer", job_type="answer_question", status="queued", queue_timestamp=None, scope="shared", owner=None, target_question_page_id="question-page")
            worker.run_job(job)
            self.assertTrue((root / "wiki" / "shared" / "faq" / "answer.md").exists())
            self.assertEqual(repository.question_updates[-1][0], "faq")
            self.assertEqual(repository.upserted_pages[-1].review_state, "in_review")
            self.assertEqual(repository.upserted_pages[-1].status, "draft")

    def test_promote_private_writes_logs_and_updates_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            ensure_owner_scope(root, "alice")
            private_path = root / "wiki" / "users" / "alice" / "concepts" / "private-note.md"
            private_path.write_text(
                "---\n"
                'title: "Private Note"\n'
                'page_type: "concept"\n'
                'slug: "private-note"\n'
                'status: "draft"\n'
                'updated_at: "2026-04-10T00:00:00Z"\n'
                "source_ids: []\n"
                "source_scope: []\n"
                "entity_keys: []\n"
                "concept_keys: []\n"
                'confidence: "medium"\n'
                "review_required: false\n"
                'scope: "private"\n'
                'owner: "alice"\n'
                'review_state: "n_a"\n'
                "promotion_origin: null\n"
                "---\n"
                "# Private Note\n\n"
                "## One-line summary\nPrivate summary.\n\n"
                "## Key points\n- One point.\n\n"
                "## Details\nDetails.\n\n"
                "## Evidence\n- Internal note.\n\n"
                "## Open questions\n\n"
                "## Related pages\n\n"
                "## Change log\n- created\n\n"
                "## Sources\n",
                encoding="utf-8",
            )
            repository = NewWorkerRepository(root)
            planner = StaticPlanner(
                response='{"schema_version":"v1","job_id":"job-promote","source_id":"promo_1","run_mode":"apply","summary":{"decision":"mixed","reason":"promote private note","review_required":true,"confidence":"medium"},"touched_paths":["wiki/shared/concepts/promoted-note.md"],"operations":[{"op":"create_file","path":"wiki/shared/concepts/promoted-note.md","page_type":"concept","reason":"promote","content":"---\\ntitle: \\"Promoted Note\\"\\npage_type: \\"concept\\"\\nslug: \\"promoted-note\\"\\nstatus: \\"published\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids: []\\nsource_scope: []\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: \\"promo_1\\"\\n---\\n# Promoted Note\\n\\n## One-line summary\\nPromoted summary.\\n\\n## Key points\\n- Shared point.\\n\\n## Details\\nRewritten details for shared scope.\\n\\n## Evidence\\n- Reviewed private material, rewritten for sharing.\\n\\n## Open questions\\n\\n## Related pages\\n\\n## Change log\\n- promoted\\n\\n## Sources\\n"}],"manifest_update":{"source_page":"wiki/shared/concepts/promoted-note.md","affected_pages":["wiki/shared/concepts/promoted-note.md"]},"warnings":[]}'
            )
            worker = Worker(repository=repository, source_fetcher=None, planner=planner, wiki_root=root, worker_name="worker")  # type: ignore[arg-type]
            job = JobRecord(page_id="job-page", job_id="job-promote", job_type="promote_private", status="queued", queue_timestamp=None, scope="shared", owner=None, target_promotion_page_id="promotion-page")
            worker.run_job(job)
            self.assertTrue((root / "wiki" / "shared" / "concepts" / "promoted-note.md").exists())
            self.assertTrue((root / "state" / "promotion_logs" / "promo_1.json").exists())
            self.assertTrue((root / "reviews" / "approved" / "promo_1.json").exists())
            self.assertTrue(repository.promotion_updates)


if __name__ == "__main__":
    unittest.main()
