from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from llmwiki_runtime.wiki_ops import (
    apply_run_plan,
    ensure_wiki_root,
    parse_run_plan,
    validate_run_plan,
)


class WikiOpsTests(unittest.TestCase):
    def test_rejects_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            plan = parse_run_plan(
                """
                {
                  "schema_version": "v1",
                  "job_id": "job_1",
                  "source_id": "src_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "mixed",
                    "reason": "bad path",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": ["../oops.md"],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "../oops.md",
                      "page_type": "concept",
                      "reason": "bad",
                      "content": "---\\ntitle: \\"Bad\\"\\npage_type: \\"concept\\"\\nslug: \\"bad\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\n---\\n# Bad\\n\\n## One-line summary\\ntext\\n\\n## Key points\\n\\n## Details\\n\\n## Evidence\\n- [S:src_1] text\\n\\n## Open questions\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_1] Example\\n"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/sources/src_1.md",
                    "affected_pages": ["../oops.md"]
                  },
                  "warnings": []
                }
                """
            )
            with self.assertRaises(ValueError):
                validate_run_plan(plan, root=root)

    def test_patch_sections_updates_existing_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            plan = parse_run_plan(
                """
                {
                  "schema_version": "v1",
                  "job_id": "job_1",
                  "source_id": "src_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "update_existing_pages",
                    "reason": "update index",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": ["wiki/index.md"],
                  "operations": [
                    {
                      "op": "patch_sections",
                      "path": "wiki/index.md",
                      "page_type": "index",
                      "reason": "record source",
                      "section_patches": [
                        {
                          "section": "## Sources",
                          "action": "append",
                          "content": "- [S:src_1] Example"
                        }
                      ]
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/sources/src_1.md",
                    "affected_pages": ["wiki/index.md"]
                  },
                  "warnings": []
                }
                """
            )
            validate_run_plan(plan, root=root)
            state = apply_run_plan(plan, root=root)
            self.assertIn("[S:src_1]", state["wiki/index.md"])


if __name__ == "__main__":
    unittest.main()
