from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from llmwiki_runtime.models import ScopeContext
from llmwiki_runtime.paths import ScopedPaths
from llmwiki_runtime.wiki_ops import (
    apply_run_plan,
    ensure_owner_scope,
    ensure_wiki_root,
    load_candidate_pages,
    load_manifest,
    load_shared_overlay_pages,
    parse_run_plan,
    update_manifest,
    validate_run_plan,
    write_run_record,
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

    def test_private_page_rejects_other_owner_path_reference(self) -> None:
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
                    "decision": "mixed",
                    "reason": "bad private reference",
                    "review_required": false,
                    "confidence": "medium"
                  },
                  "touched_paths": ["wiki/users/alice/concepts/leak.md"],
                  "operations": [
                    {
                      "op": "create_file",
                      "path": "wiki/users/alice/concepts/leak.md",
                      "page_type": "concept",
                      "reason": "bad",
                      "content": "---\\ntitle: \\"Leak\\"\\npage_type: \\"concept\\"\\nslug: \\"leak\\"\\nstatus: \\"draft\\"\\nupdated_at: \\"2026-04-10T00:00:00Z\\"\\nsource_ids:\\n  - \\"src_1\\"\\nsource_scope:\\n  - \\"private\\"\\nentity_keys: []\\nconcept_keys: []\\nconfidence: \\"medium\\"\\nreview_required: false\\nscope: \\"private\\"\\nowner: \\"alice\\"\\nreview_state: \\"n_a\\"\\npromotion_origin: null\\n---\\n# Leak\\n\\n## One-line summary\\ntext\\n\\n## Key points\\n- Reference wiki/users/bob/concepts/secret.md\\n\\n## Details\\nSee raw/users/bob/canonical/src_secret/source.md\\n\\n## Evidence\\n- [S:src_1] text\\n\\n## Open questions\\n\\n## Related pages\\n\\n## Change log\\n- created\\n\\n## Sources\\n- [S:src_1] Example\\n"
                    }
                  ],
                  "manifest_update": {
                    "source_page": "wiki/users/alice/sources/src_1.md",
                    "affected_pages": ["wiki/users/alice/concepts/leak.md"]
                  },
                  "warnings": []
                }
                """
            )
            scope_context = ScopeContext("private", "alice")
            validate_run_plan(plan, root=root, scope_context=scope_context)
            with self.assertRaises(ValueError):
                apply_run_plan(plan, root=root, scope_context=scope_context, source_scope="private")

    def test_rejects_unsafe_owner_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(ValueError):
                ScopedPaths(root, ScopeContext("private", "../../tmp")).wiki_scope_root

    def test_rejects_unsafe_source_id_in_generated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scoped_paths = ScopedPaths(root, ScopeContext("shared"))
            with self.assertRaises(ValueError):
                scoped_paths.source_page_path("../escape")
            with self.assertRaises(ValueError):
                scoped_paths.manifest_path("nested/source")

    def test_rejects_poisoned_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            manifest_path = root / "state" / "manifests" / "shared" / "src_1.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                """
                {
                  "source_id": "src_1",
                  "scope": "shared",
                  "owner": null,
                  "checksum": "sha256:test",
                  "source_page": "wiki/shared/sources/src_1.md",
                  "affected_pages": ["wiki/users/alice/concepts/oops.md"],
                  "last_job_id": "job_1",
                  "last_updated_at": "2026-04-10T00:00:00Z"
                }
                """,
                encoding="utf-8",
            )
            scoped_paths = ScopedPaths(root, ScopeContext("shared"))
            with self.assertRaises(ValueError):
                load_manifest(scoped_paths, "src_1")

    def test_validate_run_plan_rejects_non_list_affected_pages(self) -> None:
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
                  "summary": {"decision": "no_op", "reason": "noop", "review_required": false, "confidence": "medium"},
                  "touched_paths": [],
                  "operations": [{"op": "no_op", "path": "wiki/shared/sources/src_1.md", "page_type": "source", "reason": "noop"}],
                  "manifest_update": {"source_page": "wiki/shared/sources/src_1.md", "affected_pages": "bad"},
                  "warnings": []
                }
                """
            )
            with self.assertRaises(ValueError):
                validate_run_plan(plan, root=root, scope_context=ScopeContext("shared"))

    def test_validate_run_plan_rejects_bad_source_page(self) -> None:
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
                  "summary": {"decision": "no_op", "reason": "noop", "review_required": false, "confidence": "medium"},
                  "touched_paths": [],
                  "operations": [{"op": "no_op", "path": "wiki/shared/sources/src_1.md", "page_type": "source", "reason": "noop"}],
                  "manifest_update": {"source_page": "wiki/shared/concepts/not-a-source.md", "affected_pages": []},
                  "warnings": []
                }
                """
            )
            with self.assertRaises(ValueError):
                validate_run_plan(plan, root=root, scope_context=ScopeContext("shared"))

    def test_update_manifest_normalizes_and_deduplicates_affected_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            scoped_paths = ScopedPaths(root, ScopeContext("shared"))
            manifest_path = update_manifest(
                scoped_paths=scoped_paths,
                source_id="src_1",
                checksum="sha256:test",
                source_page="wiki/shared/sources/src_1.md",
                affected_pages=[
                    "wiki/shared/indexes/index.md",
                    "wiki/shared/indexes/index.md",
                    "wiki/shared/sources/src_1.md",
                ],
                job_id="job_1",
            )
            payload = load_manifest(scoped_paths, "src_1")
            self.assertEqual(
                payload["affected_pages"],
                ["wiki/shared/indexes/index.md", "wiki/shared/sources/src_1.md"],
            )
            self.assertTrue(manifest_path.exists())

    def test_load_shared_overlay_pages_loads_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            shared_source = root / "wiki" / "shared" / "sources" / "src_1.md"
            shared_source.write_text("source", encoding="utf-8")
            manifest_root = root / "state" / "manifests" / "shared"
            manifest_root.mkdir(parents=True, exist_ok=True)
            (manifest_root / "src_1.json").write_text(
                """
                {
                  "source_id": "src_1",
                  "scope": "shared",
                  "owner": null,
                  "checksum": "sha256:test",
                  "source_page": "wiki/shared/sources/src_1.md",
                  "affected_pages": ["wiki/shared/indexes/index.md", "wiki/shared/sources/src_1.md"],
                  "last_job_id": "job_1",
                  "last_updated_at": "2026-04-10T00:00:00Z"
                }
                """,
                encoding="utf-8",
            )
            pages = load_shared_overlay_pages(root)
            self.assertIn("wiki/shared/sources/src_1.md", pages)

    def test_load_shared_overlay_pages_rejects_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            manifest_root = root / "state" / "manifests" / "shared"
            manifest_root.mkdir(parents=True, exist_ok=True)
            (manifest_root / "src_1.json").write_text(
                """
                {
                  "source_id": "src_1",
                  "scope": "shared",
                  "owner": null,
                  "checksum": "sha256:test",
                  "source_page": "wiki/shared/sources/src_1.md",
                  "affected_pages": ["wiki/users/alice/concepts/oops.md"],
                  "last_job_id": "job_1",
                  "last_updated_at": "2026-04-10T00:00:00Z"
                }
                """,
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_shared_overlay_pages(root)

    def test_write_run_record_supports_failure_without_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ensure_wiki_root(root)
            scoped_paths = ScopedPaths(root, ScopeContext("shared"))
            path = write_run_record(
                scoped_paths=scoped_paths,
                job_id="job_1",
                raw_model_output="bad output",
                plan=None,
                changed=None,
                manifest_path=None,
                failure={"stage": "validating_plan", "error_class": "validation", "message": "bad"},
            )
            payload = path.read_text(encoding="utf-8")
            self.assertIn('"plan": null', payload)
            self.assertIn('"failure"', payload)


if __name__ == "__main__":
    unittest.main()
