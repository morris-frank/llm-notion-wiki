from __future__ import annotations

from collections import OrderedDict
import unittest

from llmwiki_runtime.frontmatter import dump_document, parse_document


class FrontmatterTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        metadata = OrderedDict(
            [
                ("title", "Sample"),
                ("page_type", "concept"),
                ("slug", "sample"),
                ("status", "draft"),
                ("updated_at", "2026-04-10T00:00:00Z"),
                ("source_ids", ["src_1"]),
                ("source_scope", ["shared"]),
                ("entity_keys", []),
                ("concept_keys", ["sample"]),
                ("confidence", "medium"),
                ("review_required", False),
                ("scope", "shared"),
                ("owner", None),
                ("review_state", "unreviewed"),
                ("promotion_origin", None),
            ]
        )
        document = dump_document(metadata, "# Title\n\n## One-line summary\ntext\n")
        parsed = parse_document(document)
        self.assertEqual(parsed.metadata["title"], "Sample")
        self.assertEqual(parsed.metadata["source_ids"], ["src_1"])
        self.assertEqual(parsed.metadata["source_scope"], ["shared"])
        self.assertEqual(parsed.metadata["scope"], "shared")
        self.assertFalse(parsed.metadata["review_required"])


if __name__ == "__main__":
    unittest.main()
