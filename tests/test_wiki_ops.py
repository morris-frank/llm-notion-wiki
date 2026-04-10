from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from llmwiki_runtime.models import ScopeContext
from llmwiki_runtime.wiki_ops import (
    apply_run_plan,
    ensure_owner_scope,
    ensure_wiki_root,
    parse_run_plan,
    validate_run_plan,
)


class WikiOpsTests(unittest.TestCase):
    def test_rejects_cross_scope_path(self) -> None:
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
                  "touched_paths": ["wiki/users/alice/concepts/oops.md"],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "wiki/users/alice/concepts/oops.md",
                      "page_type": "concept",
                      "reason": "bad",
                      "content": "---\\ntitle: \\"Bad\\"\\npage_type: \\"concept\\"\\nslug: \\"bad\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nsource_scope:\\n  - \\"shared\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: null\\n---\\n# Bad\\n\\n## One-line summary\\ntext\\n\\n## Key points\\n\\n## Details\\n\\n## Evidence\\n- [S:src_1] text\\n\\n## Open questions\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_1] Example\\n"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/shared/sources/src_1.md",
                    "affected_pages": ["wiki/users/alice/concepts/oops.md"]
                  },
                  "warnings": []
                }
                """
            )
            with self.assertRaises(ValueError):
                validate_run_plan(plan, root=root, scope_context=ScopeContext("shared"))

    def test_private_owner_scope_paths_and_patching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            ensure_owner_scope(root, "alice")
            plan = parse_run_plan(
                """
                {
                  "schema_version": "v1",
                  "job_id": "job_1",
                  "source_id": "src_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "update_existing_pages",
                    "reason": "update private index",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": ["wiki/users/alice/indexes/index.md"],
                  "operations": [
                    {
                      "op": "patch_sections",
                      "path": "wiki/users/alice/indexes/index.md",
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
                    "source_page": "wiki/users/alice/sources/src_1.md",
                    "affected_pages": ["wiki/users/alice/indexes/index.md"]
                  },
                  "warnings": []
                }
                """
            )
            scope_context = ScopeContext("private", "alice")
            validate_run_plan(plan, root=root, scope_context=scope_context)
            state = apply_run_plan(plan, root=root, scope_context=scope_context, source_scope="private")
            self.assertIn("[S:src_1]", state["wiki/users/alice/indexes/index.md"])

    def test_shared_page_rejects_private_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            plan = parse_run_plan(
                """
                {
                  "schema_version": "v1",
                  "job_id": "job_1",
                  "source_id": "src_private_1",
                  "run_mode": "apply",
                  "summary": {
                    "decision": "mixed",
                    "reason": "bad shared provenance",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": ["wiki/shared/sources/src_private_1.md"],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "wiki/shared/sources/src_private_1.md",
                      "page_type": "source",
                      "reason": "bad",
                      "content": "---\\ntitle: \\"Bad Shared Page\\"\\npage_type: \\"source\\"\\nslug: \\"src-private-1\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_private_1\\"\\nsource_scope:\\n  - \\"private\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"shared\\"\\nowner: null\\nreview_state: \\"unreviewed\\"\\npromotion_origin: null\\n---\\n# Bad Shared Page\\n\\n## One-line summary\\ntext\\n\\n## Source summary\\ntext [S:src_private_1]\\n\\n## Main claims\\n- claim [S:src_private_1]\\n\\n## Important entities\\n\\n## Important concepts\\n\\n## Reliability notes\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_private_1] Bad\\n"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/shared/sources/src_private_1.md",
                    "affected_pages": ["wiki/shared/sources/src_private_1.md"]
                  },
                  "warnings": []
                }
                """
            )
            scope_context = ScopeContext("shared")
            validate_run_plan(plan, root=root, scope_context=scope_context)
            with self.assertRaises(ValueError):
                apply_run_plan(plan, root=root, scope_context=scope_context, source_scope="private")


if __name__ == "__main__":
    unittest.main()
