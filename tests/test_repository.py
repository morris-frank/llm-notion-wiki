from __future__ import annotations

import unittest

from llmwiki_runtime.models import ScopeContext
from llmwiki_runtime.repository import NotionRepository


class FakeNotionClient:
    def __init__(self) -> None:
        self.query_results: list[dict] = []
        self.updated_pages: list[tuple[str, dict]] = []

    def query_data_source(self, data_source_id: str, *, filter_obj=None, sorts=None, page_size=100, start_cursor=None, filter_properties=None):
        return {"results": list(self.query_results)}

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.updated_pages.append((page_id, properties))
        return {"id": page_id, "properties": properties}


def _source_page(page_id: str, *, source_id: str, scope: str, owner: str | None) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Source ID": {"rich_text": [{"plain_text": source_id}]},
            "Source Title": {"title": [{"plain_text": source_id}]},
            "Source Type": {"select": {"name": "web_page"}},
            "Trust Level": {"select": {"name": "primary"}},
            "Source Status": {"select": {"name": "processed"}},
            "Scope": {"select": {"name": scope}},
            "Owner": {"rich_text": [] if owner is None else [{"plain_text": owner}]},
        },
    }


class RepositoryTests(unittest.TestCase):
    def test_mark_job_failed_clears_phase(self) -> None:
        client = FakeNotionClient()
        repository = NotionRepository(client, "sources", "wiki", "jobs", "policies")

        repository.mark_job_failed("job-page-id", "validation", "bad plan")

        _, props = client.updated_pages[-1]
        self.assertEqual(props["Job Status"]["select"]["name"], "failed")
        self.assertIsNone(props["Job Phase"]["select"])

    def test_resolve_backing_source_page_ids_rejects_ambiguous_private_and_shared_match(self) -> None:
        client = FakeNotionClient()
        client.query_results = [
            _source_page("shared-page", source_id="src_1", scope="shared", owner=None),
            _source_page("private-page", source_id="src_1", scope="private", owner="alice"),
        ]
        repository = NotionRepository(client, "sources", "wiki", "jobs", "policies")

        with self.assertRaises(ValueError):
            repository.resolve_backing_source_page_ids(["src_1"], page_scope_context=ScopeContext("private", "alice"))


if __name__ == "__main__":
    unittest.main()
