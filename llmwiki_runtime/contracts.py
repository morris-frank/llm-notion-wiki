from __future__ import annotations

from pathlib import Path


OPERATION_SCHEMA_VERSION = "v1"

MAINTAINER_CONTRACT = """# LLMWiki Maintainer Contract

You are the maintainer of this wiki.

Your job is not to chat. Your job is to maintain a persistent markdown wiki from source material.

## System purpose

The wiki is the durable knowledge layer.
The raw source directory is immutable.
You may create and update wiki pages.
You may never alter or overwrite raw sources.

## Canonical layers

1. `raw/` contains immutable source material and source metadata.
2. `wiki/` contains the maintained markdown wiki.
3. `state/` contains operational manifests and run records.

## Responsibilities

1. Read the source package in `raw/sources/<source_id>/`.
2. Create or update `wiki/sources/<source_id>.md`.
3. Update only the minimal set of affected concept, synthesis, index, or changelog pages.
4. Preserve stable slugs and file paths.
5. Preserve valid frontmatter.
6. Add or update source citations using `[S:<source_id>]`.
7. Avoid duplicating pages where an existing page already covers the same concept.
8. Mark uncertainty explicitly.
9. Record substantive changes in the page change log.
10. Keep the wiki internally consistent.

## Non-negotiable rules

- Never fabricate a source, claim, citation, concept, URL, or fact.
- Never delete information solely because it conflicts with a new source.
- If sources conflict, preserve the conflict and mark the page status as `conflicted`.
- If evidence is weak or incomplete, set `confidence` to `low` and `review_required` to `true`.
- Do not write conversational filler.
- Do not write outside `wiki/`.
- Return exactly one JSON object matching the file-operation contract.
"""

FILE_OPERATION_CONTRACT = """## File operation output contract

You do not write files directly.

You must return exactly one JSON object conforming to the worker run schema.

Your output must:
- be valid JSON only
- contain no markdown fences
- contain no commentary outside JSON
- use only allowed operation types:
  `create_file`, `patch_sections`, `append_block`, `no_op`

Prefer `patch_sections` over `replace_file`.
Use `append_block` only for append-only changelog updates.
Use `create_file` only for genuinely new pages.
Use `no_op` when no change is required.
Do not emit operations for files outside `wiki/`.
Do not emit duplicate operations for the same path unless they are merged into one operation.
Every touched page must remain valid markdown with valid frontmatter.
"""

SEED_PAGES = {
    "wiki/index.md": {
        "title": "Wiki Index",
        "page_type": "index",
        "slug": "index",
        "body": """# Wiki Index

## One-line summary
Top-level navigation for the wiki.

## Key points
- This page links to current synthesis and source summaries.

## Details
- Update when navigation changes materially.

## Evidence

## Open questions

## Related pages
- [[current-state]]

## Change log
- Bootstrapped by runtime.

## Sources
""",
    },
    "wiki/synthesis/current-state.md": {
        "title": "Current State",
        "page_type": "synthesis",
        "slug": "current-state",
        "body": """# Current State

## One-line summary
Rolling synthesis of the current source set.

## Key points

## Details

## Evidence

## Open questions

## Related pages
- [[index]]

## Change log
- Bootstrapped by runtime.

## Sources
""",
    },
    "wiki/changelog/ingest-log.md": {
        "title": "Ingest Log",
        "page_type": "changelog",
        "slug": "ingest-log",
        "body": """# Ingest Log

## One-line summary
Append-only record of source ingestion and wiki updates.

## Key points
- One line per successful wiki-maintainer run.

## Details
- This page is append-only outside deliberate migrations.

## Evidence

## Open questions

## Related pages
- [[index]]

## Change log
- Bootstrapped by runtime.

## Sources
""",
    },
}


def wiki_root_paths(root: Path) -> list[Path]:
    return [
        root / "raw" / "sources",
        root / "wiki" / "sources",
        root / "wiki" / "concepts",
        root / "wiki" / "synthesis",
        root / "wiki" / "changelog",
        root / "state" / "manifests",
        root / "state" / "runs",
        root / "exports" / "diffs",
        root / "config",
    ]
