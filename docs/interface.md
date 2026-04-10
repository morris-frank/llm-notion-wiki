Use a two-layer interface:
	1.	run envelope
	2.	file operations array

That keeps execution deterministic and auditable.

**Scope (llmwiki-runtime).** Every path is under `WIKI_ROOT` and scoped: `wiki/shared/...` (team) or `wiki/users/<owner>/...` (private). Examples below use the **shared** scope. Canonical layout: [shared.md](shared.md).

1. Design goals

The interface should guarantee:
	•	no arbitrary filesystem writes
	•	no mutation outside wiki/
	•	stable, minimal diffs
	•	explicit no-op support
	•	explicit conflict/review signalling
	•	worker-side validation before write

2. Exact top-level JSON shape

{
  "schema_version": "v1",
  "job_id": "job_000001",
  "source_id": "src_karpathy_llmwiki_001",
  "run_mode": "apply",
  "summary": {
    "decision": "update_existing_pages",
    "reason": "The source adds material to an existing concept page and requires a new source summary page.",
    "review_required": false,
    "confidence": "medium"
  },
  "touched_paths": [
    "wiki/shared/sources/src_karpathy_llmwiki_001.md",
    "wiki/shared/concepts/llmwiki.md",
    "wiki/shared/indexes/ingest-log.md"
  ],
  "operations": [],
  "manifest_update": {
    "source_page": "wiki/shared/sources/src_karpathy_llmwiki_001.md",
    "affected_pages": [
      "wiki/shared/sources/src_karpathy_llmwiki_001.md",
      "wiki/shared/concepts/llmwiki.md",
      "wiki/shared/indexes/ingest-log.md"
    ]
  },
  "warnings": []
}

3. Allowed run_mode

"run_mode": "apply|dry_run"

	•	apply: worker validates, writes wiki files, updates manifest, syncs Notion wiki/source rows, writes diff + run record
	•	dry_run: worker validates and writes a **run record** with `dry_run: true` and projected `changed_files`; it does **not** write wiki markdown to disk, manifest, diff patch, or call Notion wiki/source upserts. The **job row** in Notion is still marked succeeded with the run record URI as output (implementation: `llmwiki-runtime`).

4. summary object

{
  "decision": "create_new_pages|update_existing_pages|mixed|no_op|conflict_detected",
  "reason": "string",
  "review_required": true,
  "confidence": "high|medium|low"
}

Rules:
	•	decision=no_op requires operations=[]
	•	decision=conflict_detected should usually set review_required=true

5. operations array

Each element must be exactly one of these operation types:
	•	create_file
	•	replace_file
	•	patch_sections
	•	append_block
	•	no_op

No other types.

5.1 create_file

Use only when the target does not already exist.

{
  "op": "create_file",
  "path": "wiki/shared/sources/src_karpathy_llmwiki_001.md",
  "page_type": "source",
  "reason": "New source summary page.",
  "content": "---\ntitle: \"Karpathy llm-wiki gist\"\npage_type: \"source\"\nslug: \"src-karpathy-llmwiki-001\"\nstatus: \"draft\"\nupdated_at: \"2026-04-09T14:00:00Z\"\nsource_ids:\n  - \"src_karpathy_llmwiki_001\"\nentity_keys: []\nconcept_keys:\n  - \"llmwiki\"\nconfidence: \"medium\"\nreview_required: false\nsource_type: \"web_page\"\ncanonical_url: \"https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f\"\nchecksum: \"sha256:...\"\n---\n\n# Karpathy llm-wiki gist\n\n## One-line summary\n...\n",
  "content_sha256": "optional-hash-of-content"
}

5.2 replace_file

Use only when rewriting the full file is justified.

{
  "op": "replace_file",
  "path": "wiki/shared/concepts/llmwiki.md",
  "page_type": "concept",
  "reason": "Existing page needs consistent restructuring after new source integration.",
  "content": "---\n...\n",
  "previous_content_sha256": "expected-old-hash",
  "content_sha256": "optional-new-hash"
}

Worker rule:
	•	reject unless file exists
	•	reject if previous_content_sha256 is supplied and does not match actual file

5.3 patch_sections

Preferred update mode for normal page edits.

{
  "op": "patch_sections",
  "path": "wiki/shared/concepts/llmwiki.md",
  "page_type": "concept",
  "reason": "Update evidence and change log with new source-backed material.",
  "section_patches": [
    {
      "section": "## Key points",
      "action": "replace",
      "content": "- LLMWiki is framed as a maintained markdown wiki rather than a chat transcript. [S:src_karpathy_llmwiki_001]\n- The model is expected to update multiple pages when ingesting one source. [S:src_karpathy_llmwiki_001]"
    },
    {
      "section": "## Evidence",
      "action": "append",
      "content": "- [S:src_karpathy_llmwiki_001] Describes a raw source layer, a maintained markdown wiki layer, and a schema/instructions layer."
    },
    {
      "section": "## Change log",
      "action": "append",
      "content": "- 2026-04-09: updated with source src_karpathy_llmwiki_001"
    },
    {
      "section": "## Sources",
      "action": "upsert_bullet",
      "match_key": "[S:src_karpathy_llmwiki_001]",
      "content": "- [S:src_karpathy_llmwiki_001] Karpathy llm-wiki gist. https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
    }
  ],
  "previous_content_sha256": "expected-old-hash"
}

Allowed section actions:
	•	replace
	•	append
	•	prepend
	•	upsert_bullet

No regex-based edits from the model. Section-level edits are much safer.

5.4 append_block

Use only for append-only logs.

{
  "op": "append_block",
  "path": "wiki/shared/indexes/ingest-log.md",
  "page_type": "changelog",
  "reason": "Record ingest event.",
  "content": "- 2026-04-09T14:00:00Z | job_000001 | src_karpathy_llmwiki_001 | updated wiki/shared/sources/src_karpathy_llmwiki_001.md, wiki/shared/concepts/llmwiki.md"
}

5.5 no_op

Use when no file changes are needed.

{
  "op": "no_op",
  "path": "wiki/shared/indexes/index.md",
  "page_type": "index",
  "reason": "Navigation remains sufficient; no update required."
}

6. page_type enum

Every operation must declare one of:

"page_type": "source|concept|entity|faq|question|synthesis|index|changelog"

Worker should reject any other value.

7. Path rules

Worker must enforce:
	•	path must start with wiki/
	•	path must end with .md
	•	no ..
	•	no absolute paths
	•	path directory must match page_type

Examples (shared scope):
	•	page_type=source → `wiki/shared/sources/*.md`
	•	page_type=concept → `wiki/shared/concepts/*.md`
	•	page_type=entity → `wiki/shared/entities/*.md`
	•	page_type=faq → `wiki/shared/faq/*.md`
	•	page_type=question → `wiki/shared/open_questions/*.md`
	•	page_type=synthesis → `wiki/shared/synthesis/*.md`
	•	page_type=index → `wiki/shared/indexes/index.md`
	•	page_type=changelog → `wiki/shared/indexes/ingest-log.md`

Private scope uses the same page types under `wiki/users/<owner>/...` (seeded layout has no private `entities/` root).

8. Full JSON Schema

This is a practical strict schema for worker validation.

{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LLMWikiWorkerRun",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "job_id",
    "source_id",
    "run_mode",
    "summary",
    "touched_paths",
    "operations",
    "manifest_update",
    "warnings"
  ],
  "properties": {
    "schema_version": {
      "const": "v1"
    },
    "job_id": {
      "type": "string",
      "minLength": 1
    },
    "source_id": {
      "type": "string",
      "minLength": 1
    },
    "run_mode": {
      "enum": ["apply", "dry_run"]
    },
    "summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["decision", "reason", "review_required", "confidence"],
      "properties": {
        "decision": {
          "enum": [
            "create_new_pages",
            "update_existing_pages",
            "mixed",
            "no_op",
            "conflict_detected"
          ]
        },
        "reason": { "type": "string" },
        "review_required": { "type": "boolean" },
        "confidence": { "enum": ["high", "medium", "low"] }
      }
    },
    "touched_paths": {
      "type": "array",
      "items": { "type": "string" },
      "uniqueItems": true
    },
    "operations": {
      "type": "array",
      "items": {
        "oneOf": [
          { "$ref": "#/$defs/createFileOp" },
          { "$ref": "#/$defs/replaceFileOp" },
          { "$ref": "#/$defs/patchSectionsOp" },
          { "$ref": "#/$defs/appendBlockOp" },
          { "$ref": "#/$defs/noOp" }
        ]
      }
    },
    "manifest_update": {
      "type": "object",
      "additionalProperties": false,
      "required": ["source_page", "affected_pages"],
      "properties": {
        "source_page": { "type": "string" },
        "affected_pages": {
          "type": "array",
          "items": { "type": "string" },
          "uniqueItems": true
        }
      }
    },
    "warnings": {
      "type": "array",
      "items": { "type": "string" }
    }
  },
  "$defs": {
    "baseOp": {
      "type": "object",
      "additionalProperties": false,
      "required": ["op", "path", "page_type", "reason"],
      "properties": {
        "op": { "type": "string" },
        "path": { "type": "string" },
        "page_type": {
          "enum": [
            "source",
            "concept",
            "entity",
            "question",
            "faq",
            "synthesis",
            "index",
            "changelog"
          ]
        },
        "reason": { "type": "string" }
      }
    },
    "createFileOp": {
      "allOf": [
        { "$ref": "#/$defs/baseOp" },
        {
          "properties": {
            "op": { "const": "create_file" },
            "content": { "type": "string" },
            "content_sha256": { "type": "string" }
          },
          "required": ["content"]
        }
      ]
    },
    "replaceFileOp": {
      "allOf": [
        { "$ref": "#/$defs/baseOp" },
        {
          "properties": {
            "op": { "const": "replace_file" },
            "content": { "type": "string" },
            "previous_content_sha256": { "type": "string" },
            "content_sha256": { "type": "string" }
          },
          "required": ["content"]
        }
      ]
    },
    "patchSectionsOp": {
      "allOf": [
        { "$ref": "#/$defs/baseOp" },
        {
          "properties": {
            "op": { "const": "patch_sections" },
            "previous_content_sha256": { "type": "string" },
            "section_patches": {
              "type": "array",
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["section", "action", "content"],
                "properties": {
                  "section": { "type": "string" },
                  "action": {
                    "enum": ["replace", "append", "prepend", "upsert_bullet"]
                  },
                  "match_key": { "type": "string" },
                  "content": { "type": "string" }
                }
              }
            }
          },
          "required": ["section_patches"]
        }
      ]
    },
    "appendBlockOp": {
      "allOf": [
        { "$ref": "#/$defs/baseOp" },
        {
          "properties": {
            "op": { "const": "append_block" },
            "content": { "type": "string" }
          },
          "required": ["content"]
        }
      ]
    },
    "noOp": {
      "allOf": [
        { "$ref": "#/$defs/baseOp" },
        {
          "properties": {
            "op": { "const": "no_op" }
          }
        }
      ]
    }
  }
}

9. Worker-side validation rules beyond schema

The JSON schema is not enough. The worker should also enforce:

Structural
	•	touched_paths must equal the set of non-no_op paths in operations
	•	manifest_update.affected_pages must be a superset of touched_paths
	•	summary.decision=no_op implies only no_op operations or empty operations

File safety
	•	only wiki/ paths allowed
	•	no path traversal
	•	create_file fails if file already exists
	•	replace_file fails if file missing
	•	patch_sections fails if required section missing unless worker explicitly supports section creation
	•	append_block allowed only for changelog paths

Content safety
	•	frontmatter required for create_file and replace_file on normal page types
	•	frontmatter page_type must match operation page_type
	•	frontmatter source_ids must contain current source_id
	•	all citations [S:...] used in content must appear in ## Sources

Diff minimisation
	•	prefer patch_sections over replace_file
	•	reject replace_file when only changelog append is needed

10. Recommended prompt instruction for this interface

Add this exact section to AGENTS.md:

## File operation output contract

You do not write files directly.

You must return exactly one JSON object conforming to the worker run schema.

Your output must:
- be valid JSON only
- contain no markdown fences
- contain no commentary outside JSON
- use only allowed operation types:
  `create_file`, `replace_file`, `patch_sections`, `append_block`, `no_op`

Prefer `patch_sections` over `replace_file`.

Use `append_block` only for append-only changelog updates.

Use `create_file` only for genuinely new pages.

Use `no_op` when no change is required.

Do not emit operations for files outside `wiki/`.

Do not emit duplicate operations for the same path unless they are merged into one operation.

Every touched page must remain valid markdown with valid frontmatter.

11. Example complete LLM response

{
  "schema_version": "v1",
  "job_id": "job_000001",
  "source_id": "src_karpathy_llmwiki_001",
  "run_mode": "apply",
  "summary": {
    "decision": "mixed",
    "reason": "The source requires a new source summary page and updates to an existing concept page and the changelog.",
    "review_required": false,
    "confidence": "medium"
  },
  "touched_paths": [
    "wiki/shared/sources/src_karpathy_llmwiki_001.md",
    "wiki/shared/concepts/llmwiki.md",
    "wiki/shared/indexes/ingest-log.md"
  ],
  "operations": [
    {
      "op": "create_file",
      "path": "wiki/shared/sources/src_karpathy_llmwiki_001.md",
      "page_type": "source",
      "reason": "New source summary page.",
      "content": "---\ntitle: \"Karpathy llm-wiki gist\"\npage_type: \"source\"\nslug: \"src-karpathy-llmwiki-001\"\nstatus: \"draft\"\nupdated_at: \"2026-04-09T14:00:00Z\"\nsource_ids:\n  - \"src_karpathy_llmwiki_001\"\nentity_keys: []\nconcept_keys:\n  - \"llmwiki\"\nconfidence: \"medium\"\nreview_required: false\nsource_type: \"web_page\"\ncanonical_url: \"https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f\"\nchecksum: \"sha256:...\"\n---\n\n# Karpathy llm-wiki gist\n\n## One-line summary\nA source page describing the LLMWiki pattern as a maintained markdown knowledge layer.\n\n## Source summary\n...\n\n## Main claims\n- The wiki should be a maintained directory of markdown files. [S:src_karpathy_llmwiki_001]\n\n## Important entities\n- Andrej Karpathy [S:src_karpathy_llmwiki_001]\n\n## Important concepts\n- LLMWiki [S:src_karpathy_llmwiki_001]\n\n## Reliability notes\n- Primary source authored by the originator of the concept. [S:src_karpathy_llmwiki_001]\n\n## Related pages\n- [[llmwiki]]\n\n## Change log\n- 2026-04-09: created from source src_karpathy_llmwiki_001\n\n## Sources\n- [S:src_karpathy_llmwiki_001] Karpathy llm-wiki gist. https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f\n"
    },
    {
      "op": "patch_sections",
      "path": "wiki/shared/concepts/llmwiki.md",
      "page_type": "concept",
      "reason": "Add new evidence and update change log.",
      "section_patches": [
        {
          "section": "## Evidence",
          "action": "append",
          "content": "- [S:src_karpathy_llmwiki_001] Frames the system as raw sources plus a maintained markdown wiki plus instructions."
        },
        {
          "section": "## Sources",
          "action": "upsert_bullet",
          "match_key": "[S:src_karpathy_llmwiki_001]",
          "content": "- [S:src_karpathy_llmwiki_001] Karpathy llm-wiki gist. https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
        },
        {
          "section": "## Change log",
          "action": "append",
          "content": "- 2026-04-09: updated with source src_karpathy_llmwiki_001"
        }
      ]
    },
    {
      "op": "append_block",
      "path": "wiki/shared/indexes/ingest-log.md",
      "page_type": "changelog",
      "reason": "Record the ingest run.",
      "content": "- 2026-04-09T14:00:00Z | job_000001 | src_karpathy_llmwiki_001 | created wiki/shared/sources/src_karpathy_llmwiki_001.md; updated wiki/shared/concepts/llmwiki.md"
    }
  ],
  "manifest_update": {
    "source_page": "wiki/shared/sources/src_karpathy_llmwiki_001.md",
    "affected_pages": [
      "wiki/shared/sources/src_karpathy_llmwiki_001.md",
      "wiki/shared/concepts/llmwiki.md",
      "wiki/shared/indexes/ingest-log.md"
    ]
  },
  "warnings": []
}

12. Recommendation for v1

For v1, permit only:
	•	create_file
	•	patch_sections
	•	append_block
	•	no_op

and reject replace_file.

That is the safer default. Full-file replacement is where most maintainers become unstable and generate large unnecessary diffs.