from __future__ import annotations

import copy
import unittest

from llmwiki_runtime.models import ScopeContext, WikiPageMetadata
from llmwiki_runtime.repository import NotionRepository


def _rich_text_nodes(value: str | None) -> list[dict]:
    if value is None:
        return []
    return [{"plain_text": value}]


def _title_nodes(value: str) -> list[dict]:
    return [{"plain_text": value}]


def _source_page(page_id: str, *, source_id: str, scope: str, owner: str | None) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Source ID": {"rich_text": _rich_text_nodes(source_id)},
            "Source Title": {"title": _title_nodes(source_id)},
            "Source Type": {"select": {"name": "web_page"}},
            "Trust Level": {"select": {"name": "primary"}},
            "Source Status": {"select": {"name": "processed"}},
            "Scope": {"select": {"name": scope}},
            "Owner": {"rich_text": _rich_text_nodes(owner)},
        },
    }


def _job_page(
    page_id: str,
    *,
    job_id: str = "job_1",
    status: str = "queued",
    locked: bool = False,
    worker_name: str = "",
    started_at: str | None = None,
    attempt_count: int = 0,
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Job ID": {"rich_text": _rich_text_nodes(job_id)},
            "Job Type": {"select": {"name": "ingest_source"}},
            "Job Status": {"select": {"name": status}},
            "Queue Timestamp": {"date": {"start": "2026-04-10T00:00:00Z"}},
            "Locked": {"checkbox": locked},
            "Worker Name": {"rich_text": _rich_text_nodes(worker_name)},
            "Started At": {"date": {"start": started_at}} if started_at else {"date": None},
            "Scope": {"select": {"name": "shared"}},
            "Owner": {"rich_text": []},
            "Attempt Count": {"number": attempt_count},
            "Target Source": {"relation": [{"id": "source-page-id"}]},
            "Target Wiki Page": {"relation": []},
            "Idempotency Key": {"rich_text": _rich_text_nodes("key-1")},
            "Policy Version Ref": {"relation": []},
        },
    }


def _policy_page(page_id: str, *, policy_scope: str, owner: str | None = None) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Active": {"checkbox": True},
            "Policy Target Scope": {"select": {"name": policy_scope}},
            "Policy Owner": {"rich_text": _rich_text_nodes(owner)},
        },
    }


class FakeNotionClient:
    def __init__(self) -> None:
        self.query_results: list[dict] = []
        self.updated_pages: list[tuple[str, dict]] = []
        self.created_pages: list[tuple[str, dict]] = []
        self.retrieved_pages: dict[str, dict] = {}
        self.query_calls: list[tuple[str, dict | None, list[dict] | None, int, str | None, list[str] | None]] = []
        self.create_counter = 0
        self.existing_jobs_by_key: dict[str, dict] = {}

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_obj=None,
        sorts=None,
        page_size=100,
        start_cursor=None,
        filter_properties=None,
    ):
        self.query_calls.append((data_source_id, filter_obj, sorts, page_size, start_cursor, filter_properties))
        if data_source_id == "jobs" and filter_obj == {"property": "Idempotency Key", "rich_text": {"equals": "existing-key"}}:
            return {"results": [copy.deepcopy(self.existing_jobs_by_key["existing-key"])]}
        return {"results": copy.deepcopy(self.query_results)}

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.updated_pages.append((page_id, copy.deepcopy(properties)))
        page = self.retrieved_pages.setdefault(page_id, {"id": page_id, "properties": {}})
        page["properties"].update(copy.deepcopy(properties))
        return {"id": page_id, "properties": properties}

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        self.create_counter += 1
        page_id = f"created-{self.create_counter}"
        payload = copy.deepcopy(properties)
        self.created_pages.append((data_source_id, payload))
        page = {"id": page_id, "properties": payload}
        self.retrieved_pages[page_id] = copy.deepcopy(page)
        return page

    def retrieve_page(self, page_id: str) -> dict:
        return copy.deepcopy(self.retrieved_pages[page_id])


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeNotionClient()
        self.repository = NotionRepository(self.client, "sources", "wiki", "jobs", "policies")

    def test_mark_job_failed_clears_phase(self) -> None:
        self.repository.mark_job_failed("job-page-id", "validation", "bad plan")

        _, props = self.client.updated_pages[-1]
        self.assertEqual(props["Job Status"]["select"]["name"], "failed")
        self.assertIsNone(props["Job Phase"]["select"])

    def test_claim_job_success_returns_started_at(self) -> None:
        job = self.repository._job_from_page(_job_page("job-page-id"))
        started_at = "2026-04-10T12:00:00Z"
        self.client.retrieved_pages["job-page-id"] = _job_page(
            "job-page-id",
            status="running",
            locked=True,
            worker_name="worker#token",
            started_at=started_at,
        )

        def update_side_effect(page_id: str, properties: dict) -> dict:
            self.client.updated_pages.append((page_id, copy.deepcopy(properties)))
            self.client.retrieved_pages[page_id] = _job_page(
                page_id,
                status="running",
                locked=True,
                worker_name=properties["Worker Name"]["rich_text"][0]["text"]["content"],
                started_at=properties["Started At"]["date"]["start"],
            )
            return {"id": page_id, "properties": properties}

        self.client.update_page = update_side_effect  # type: ignore[method-assign]
        claimed_at = self.repository.claim_job(job, "worker")
        self.assertIsNotNone(claimed_at)
        self.assertEqual(self.client.updated_pages[-1][1]["Job Status"]["select"]["name"], "running")

    def test_claim_job_lost_returns_none(self) -> None:
        job = self.repository._job_from_page(_job_page("job-page-id"))

        def update_side_effect(page_id: str, properties: dict) -> dict:
            self.client.updated_pages.append((page_id, copy.deepcopy(properties)))
            self.client.retrieved_pages[page_id] = _job_page(
                page_id,
                status="running",
                locked=True,
                worker_name="other-worker",
                started_at=properties["Started At"]["date"]["start"],
            )
            return {"id": page_id, "properties": properties}

        self.client.update_page = update_side_effect  # type: ignore[method-assign]
        self.assertIsNone(self.repository.claim_job(job, "worker"))

    def test_create_job_returns_existing_idempotent_match(self) -> None:
        self.client.existing_jobs_by_key["existing-key"] = _job_page("existing-page", job_id="job_existing")
        job = self.repository.create_job(
            job_type="ingest_source",
            title="Ingest Source",
            target_source_page_id="source-page-id",
            idempotency_key="existing-key",
            scope_context=ScopeContext("shared"),
        )
        self.assertEqual(job.page_id, "existing-page")
        self.assertFalse(self.client.created_pages)

    def test_requeue_job_resets_status_and_increments_attempt(self) -> None:
        self.client.retrieved_pages["job-page-id"] = _job_page(
            "job-page-id",
            status="failed",
            locked=True,
            attempt_count=2,
        )
        job = self.repository.requeue_job("job-page-id")
        _, props = self.client.updated_pages[-1]
        self.assertEqual(props["Job Status"]["select"]["name"], "queued")
        self.assertIsNone(props["Job Phase"]["select"])
        self.assertFalse(props["Locked"]["checkbox"])
        self.assertEqual(props["Attempt Count"]["number"], 3)
        self.assertEqual(job.status, "queued")

    def test_active_policy_page_id_prefers_owner_specific_private_policy(self) -> None:
        self.client.query_results = [
            _policy_page("policy-all", policy_scope="all"),
            _policy_page("policy-alice", policy_scope="private", owner="alice"),
            _policy_page("policy-bob", policy_scope="private", owner="bob"),
        ]
        self.assertEqual(self.repository.active_policy_page_id(ScopeContext("private", "alice")), "policy-alice")
        self.assertEqual(self.repository.active_policy_page_id(ScopeContext("shared")), "policy-all")

    def test_active_policy_page_id_ignores_incompatible_first_row(self) -> None:
        self.client.query_results = [
            _policy_page("policy-bob", policy_scope="private", owner="bob"),
            _policy_page("policy-all", policy_scope="all"),
        ]
        self.assertEqual(self.repository.active_policy_page_id(ScopeContext("private", "alice")), "policy-all")
        self.assertEqual(self.repository.active_policy_page_id(ScopeContext("shared")), "policy-all")

    def test_active_policy_page_id_returns_none_when_no_compatible_policy_exists(self) -> None:
        self.client.query_results = [
            _policy_page("policy-bob", policy_scope="private", owner="bob"),
        ]
        self.assertIsNone(self.repository.active_policy_page_id(ScopeContext("private", "alice")))
        self.assertIsNone(self.repository.active_policy_page_id(ScopeContext("shared")))

    def test_upsert_wiki_page_creates_when_missing(self) -> None:
        self.client.query_results = []
        metadata = WikiPageMetadata(
            path="wiki/shared/sources/src_1.md",
            title="Source",
            slug="src-1",
            page_type="source",
            status="draft",
            confidence="medium",
            review_required=False,
            source_ids=["src_1", "src_2"],
            source_scope=["shared"],
            scope="shared",
            owner=None,
            review_state="unreviewed",
            promotion_origin=None,
            summary="Summary",
        )
        self.repository.upsert_wiki_page(
            metadata,
            backing_source_page_ids=["source-a", "source-b"],
            latest_job_page_id="job-page-id",
        )
        self.assertEqual(self.client.created_pages[-1][0], "wiki")
        props = self.client.created_pages[-1][1]
        self.assertEqual([item["id"] for item in props["Backing Sources"]["relation"]], ["source-a", "source-b"])

    def test_upsert_wiki_page_updates_when_existing(self) -> None:
        self.client.query_results = [
            {
                "id": "wiki-page-id",
                "properties": {
                    "Wiki Slug": {"rich_text": _rich_text_nodes("src-1")},
                    "Scope": {"select": {"name": "shared"}},
                },
            }
        ]
        metadata = WikiPageMetadata(
            path="wiki/shared/sources/src_1.md",
            title="Source",
            slug="src-1",
            page_type="source",
            status="draft",
            confidence="medium",
            review_required=False,
            source_ids=["src_1"],
            source_scope=["shared"],
            scope="shared",
            owner=None,
            review_state="unreviewed",
            promotion_origin=None,
            summary="Summary",
        )
        self.repository.upsert_wiki_page(
            metadata,
            backing_source_page_ids=["source-a"],
            latest_job_page_id="job-page-id",
        )
        self.assertEqual(self.client.updated_pages[-1][0], "wiki-page-id")
        props = self.client.updated_pages[-1][1]
        self.assertEqual(props["Latest Job"]["relation"][0]["id"], "job-page-id")

    def test_update_source_after_wiki_marks_processed_and_clears_regeneration(self) -> None:
        source = self.repository._source_from_page(_source_page("source-page", source_id="src_1", scope="shared", owner=None))
        self.repository.update_source_after_wiki(source, source_summary_pointer="file:///tmp/source.md")
        _, props = self.client.updated_pages[-1]
        self.assertEqual(props["Source Status"]["select"]["name"], "processed")
        self.assertFalse(props["Trigger Regeneration"]["checkbox"])
        self.assertEqual(props["Source Summary Pointer"]["url"], "file:///tmp/source.md")

    def test_resolve_backing_source_page_ids_rejects_ambiguous_private_and_shared_match(self) -> None:
        self.client.query_results = [
            _source_page("shared-page", source_id="src_1", scope="shared", owner=None),
            _source_page("private-page", source_id="src_1", scope="private", owner="alice"),
        ]
        with self.assertRaises(ValueError):
            self.repository.resolve_backing_source_page_ids(["src_1"], page_scope_context=ScopeContext("private", "alice"))


if __name__ == "__main__":
    unittest.main()
