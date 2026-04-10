from __future__ import annotations

import io
import json
from unittest import mock
from urllib import error
import unittest

from llmwiki_runtime.notion import NotionAPIError, NotionClient, normalize_notion_id, notion_page_id_from_reference


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


class NotionClientTests(unittest.TestCase):
    def test_normalize_notion_id_accepts_raw_and_dashed(self) -> None:
        self.assertEqual(
            normalize_notion_id("0123456789abcdef0123456789abcdef"),
            "01234567-89ab-cdef-0123-456789abcdef",
        )
        self.assertEqual(
            normalize_notion_id("01234567-89ab-cdef-0123-456789abcdef"),
            "01234567-89ab-cdef-0123-456789abcdef",
        )

    def test_notion_page_id_from_reference_extracts_id_from_url(self) -> None:
        self.assertEqual(
            notion_page_id_from_reference("https://www.notion.so/workspace/Target-Page-0123456789abcdef0123456789abcdef"),
            "01234567-89ab-cdef-0123-456789abcdef",
        )
        self.assertIsNone(notion_page_id_from_reference("https://example.com/not-a-notion-id"))
        self.assertIsNone(notion_page_id_from_reference("not-a-page-id"))

    @mock.patch("llmwiki_runtime.notion.request.urlopen")
    def test_request_raises_notion_api_error_with_body(self, mock_urlopen) -> None:
        client = NotionClient(token="token", version="2026-03-11", api_base="https://api.notion.com/v1")
        mock_urlopen.side_effect = error.HTTPError(
            "https://api.notion.com/v1/pages/page-id",
            400,
            "bad request",
            {},
            io.BytesIO(b'{"message":"bad payload"}'),
        )
        with self.assertRaises(NotionAPIError) as ctx:
            client.retrieve_page("page-id")
        self.assertIn('{"message":"bad payload"}', str(ctx.exception))

    @mock.patch("llmwiki_runtime.notion.request.urlopen")
    def test_query_data_source_encodes_filter_properties(self, mock_urlopen) -> None:
        captured = {}

        def fake_urlopen(req):
            captured["full_url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            return _Response({"results": []})

        mock_urlopen.side_effect = fake_urlopen
        client = NotionClient(token="token", version="2026-03-11", api_base="https://api.notion.com/v1")
        client.query_data_source(
            "sources",
            filter_obj={"property": "Job Status", "select": {"equals": "queued"}},
            filter_properties=["Scope", "Owner"],
            page_size=25,
        )
        self.assertIn("filter_properties%5B%5D=Scope", captured["full_url"])
        self.assertIn("filter_properties%5B%5D=Owner", captured["full_url"])
        self.assertEqual(captured["data"]["page_size"], 25)


if __name__ == "__main__":
    unittest.main()
