from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from llmwiki_runtime.models import SourceRecord
from llmwiki_runtime.sources import SourceFetcher, assert_public_http_url
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


class _HTTPResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


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

    @mock.patch("llmwiki_runtime.sources.request.urlopen")
    def test_web_page_fetch_writes_all_artifacts_and_headers(self, mock_urlopen) -> None:
        captured = {}

        def fake_urlopen(req):
            captured["headers"] = dict(req.header_items())
            captured["url"] = req.full_url
            return _HTTPResponse("<html><head><title>Fetched Title</title></head><body><p>Hello</p><p>World</p></body></html>")

        mock_urlopen.side_effect = fake_urlopen
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            fetcher = SourceFetcher(StubNotionClient(), root)
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Example Source",
                canonical_url="https://example.com/page",
                trust_level="primary",
                status="queued",
            )
            artifacts = fetcher.fetch(source)
            self.assertEqual(captured["url"], "https://example.com/page")
            self.assertIn("Mozilla/5.0", captured["headers"]["User-agent"])
            self.assertIn("text/html", captured["headers"]["Accept"])
            self.assertTrue((artifacts.storage_dir / "metadata.json").exists())
            self.assertTrue((artifacts.storage_dir / "source.txt").exists())
            self.assertTrue((artifacts.storage_dir / "source.md").exists())
            self.assertEqual(json.loads((artifacts.storage_dir / "metadata.json").read_text(encoding="utf-8"))["title"], "Fetched Title")

    @mock.patch("llmwiki_runtime.sources.request.urlopen")
    def test_web_page_fetch_falls_back_to_source_title(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _HTTPResponse("<html><body><p>Hello</p></body></html>")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            fetcher = SourceFetcher(StubNotionClient(), root)
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Fallback Title",
                canonical_url="https://example.com/page",
                trust_level="primary",
                status="queued",
            )
            artifacts = fetcher.fetch(source)
            self.assertIn("# Fallback Title", artifacts.markdown)

    def test_assert_public_http_url_rejects_file_scheme(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_http_url("file:///etc/passwd")

    def test_assert_public_http_url_rejects_loopback_literal(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_http_url("http://127.0.0.1/path")

    def test_assert_public_http_url_rejects_private_literal(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_http_url("http://192.168.0.1/path")

    def test_web_page_missing_canonical_url_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            fetcher = SourceFetcher(StubNotionClient(), root)
            source = SourceRecord(
                page_id="source-page-id",
                source_id="src_1",
                source_type="web_page",
                title="Fallback Title",
                canonical_url=None,
                trust_level="primary",
                status="queued",
            )
            with self.assertRaises(ValueError):
                fetcher.fetch(source)


if __name__ == "__main__":
    unittest.main()
