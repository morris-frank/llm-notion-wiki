from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from llmwiki_runtime.models import SourceRecord
from llmwiki_runtime.sources import SourceFetcher
from llmwiki_runtime.wiki_ops import ensure_wiki_root


class StubNotionClient:
    def __init__(self) -> None:
        self.page_ids: list[str] = []
        self.block_ids: list[str] = []

    def retrieve_page(self, page_id: str):
        self.page_ids.append(page_id)
        return {"properties": {"title": {"title": [{"plain_text": "Target Page"}]}}}

    def retrieve_block_children(self, block_id: str, start_cursor: str | None = None):
        self.block_ids.append(block_id)
        return {
            "results": [
                {
                    "id": "block-1",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "Body"}]},
                    "has_children": False,
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }


class SourceFetcherTests(unittest.TestCase):
    def test_notion_page_uses_target_reference_not_source_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            client = StubNotionClient()
            fetcher = SourceFetcher(client, root)
            source = SourceRecord(
                page_id="source-row-page-id",
                source_id="src_1",
                source_type="notion_page",
                title="Example",
                canonical_url="https://www.notion.so/workspace/Target-Page-0123456789abcdef0123456789abcdef",
                trust_level="primary",
                status="queued",
            )
            artifacts = fetcher.fetch(source)
            self.assertEqual(client.page_ids, ["01234567-89ab-cdef-0123-456789abcdef"])
            self.assertEqual(client.block_ids, ["01234567-89ab-cdef-0123-456789abcdef"])
            self.assertEqual(artifacts.metadata["notion_page_id"], "01234567-89ab-cdef-0123-456789abcdef")


if __name__ == "__main__":
    unittest.main()
