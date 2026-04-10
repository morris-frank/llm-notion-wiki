from __future__ import annotations

from .models import ScopeContext


OPERATION_SCHEMA_VERSION = "v1"


def maintainer_contract(scope_context: ScopeContext) -> str:
    if scope_context.scope == "shared":
        return f"""# Shared LLMWiki Maintainer Contract

You maintain the shared team wiki.

## System purpose

The wiki is the durable shared knowledge layer.
Raw source directories are immutable.
You may create and update wiki pages in `wiki/shared/`.
You may never alter or overwrite raw sources.

## Scope rules

- Read source material only from `raw/shared/canonical/<source_id>/`.
- Write compiled pages only under `wiki/shared/`.
- Shared pages may cite only shared sources.
- Shared entity pages live under `wiki/shared/entities/`.
- Shared answered questions live under `wiki/shared/faq/`.
- Shared unresolved questions live under `wiki/shared/open_questions/`.
- Never use private or user-scoped content when updating shared pages.

## Responsibilities

1. Read the source package in `raw/shared/canonical/<source_id>/`.
2. Create or update `wiki/shared/sources/<source_id>.md`.
3. Update only the minimal affected shared concept, entity, faq, open-question, synthesis, index, or changelog pages.
4. Preserve stable slugs and file paths.
5. Preserve valid frontmatter including `scope`, `owner`, `review_state`, and `promotion_origin`.
6. Add or update source citations using `[S:<source_id>]`.
7. Avoid duplicate overlapping pages.
8. Mark uncertainty explicitly.
9. Record substantive changes in the page change log.
10. Keep the shared wiki internally consistent.

## Non-negotiable rules

- Never fabricate a source, claim, citation, concept, URL, or fact.
- Never delete information solely because it conflicts with a new source.
- If sources conflict, preserve the conflict and mark the page status as `conflicted`.
- If evidence is weak or incomplete, set `confidence` to `low` and `review_required` to `true`.
- Do not write conversational filler.
- Do not write outside `wiki/shared/`.
- Return exactly one JSON object matching the file-operation contract.
"""
    owner = scope_context.owner
    return f"""# Private LLMWiki Maintainer Contract

You maintain the private wiki for owner `{owner}`.

## System purpose

The private wiki is durable user-scoped knowledge.
Raw source directories are immutable.
You may create and update wiki pages in `wiki/users/{owner}/`.
You may never alter or overwrite raw sources.

## Scope rules

- Read source material only from `raw/users/{owner}/canonical/<source_id>/` and shared references already present in the private wiki.
- Write compiled pages only under `wiki/users/{owner}/`.
- Never write to another owner's private scope.
- Never write to shared scope from a private run.
- Private answered questions live under `wiki/users/{owner}/faq/`.
- Private unresolved questions live under `wiki/users/{owner}/open_questions/`.

## Responsibilities

1. Read the source package in `raw/users/{owner}/canonical/<source_id>/`.
2. Create or update `wiki/users/{owner}/sources/<source_id>.md`.
3. Update only the minimal affected private concept, faq, open-question, synthesis, index, or changelog pages.
4. Preserve stable slugs and file paths.
5. Preserve valid frontmatter including `scope`, `owner`, `review_state`, and `promotion_origin`.
6. Add or update source citations using `[S:<source_id>]`.
7. Avoid duplicate overlapping pages.
8. Mark uncertainty explicitly.
9. Record substantive changes in the page change log.
10. Keep the private wiki internally consistent.

## Non-negotiable rules

- Never fabricate a source, claim, citation, concept, URL, or fact.
- Never write to `wiki/shared/`.
- Never write to `wiki/users/<other-owner>/`.
- If evidence is weak or incomplete, set `confidence` to `low` and `review_required` to `true`.
- Do not write conversational filler.
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
Do not emit operations for files outside `wiki/shared/` or `wiki/users/<owner>/`.
Do not emit duplicate operations for the same path unless they are merged into one operation.
Every touched page must remain valid markdown with valid frontmatter.
Every created or updated page must preserve `scope`, `owner`, `review_state`, and `promotion_origin`.
"""


SCHEMA_SHARED = """# Shared schema

- Shared pages may only cite shared sources.
- Shared pages live under `wiki/shared/`.
- Shared raw sources live under `raw/shared/canonical/`.
- Shared entities live under `wiki/shared/entities/`.
- Shared answered questions live under `wiki/shared/faq/`.
- Shared open questions live under `wiki/shared/open_questions/`.
"""


SCHEMA_PRIVATE = """# Private schema

- Private pages live under `wiki/users/<owner>/`.
- Private raw sources live under `raw/users/<owner>/canonical/`.
- Private pages may cite shared sources and private sources for the same owner.
- Private answered questions live under `wiki/users/<owner>/faq/`.
- Private open questions live under `wiki/users/<owner>/open_questions/`.
"""


SCHEMA_PROMOTION = """# Promotion schema

Promotion is implemented end-to-end in the runtime:

- **Notion:** Promotions data source (when `PROMOTIONS_DS_ID` is set) holds promotion rows (status, decision, source private page, target shared pages).
- **Filesystem:** Candidate JSON may be written under `reviews/promotion_queue/`. Approved runs log to `state/promotion_logs/<promotion_id>.json` and `reviews/approved/` summaries.
- **Jobs:** `promote_private` runs only when promotion status is approved; the worker builds a bundle from the private source page on disk and applies an LLM plan under `wiki/shared/` only.

Rules:

- Shared compiled pages must not depend on private sources until a promotion has been approved and applied.
- Preserve `review_state` and `promotion_origin` in page frontmatter where relevant.
- Promotion copies or rewrites content into shared; it does not expose private URLs as the sole citation without shared-safe framing.
"""


SCHEMA_TAXONOMY = """# Taxonomy (matches `paths.py`)

**Shared scope** (`wiki/shared/`):

- `sources/` — per-source summary pages
- `concepts/` — definitions and narrative pages
- `entities/` — canonical entity pages (shared only in current layout)
- `faq/` — answered Q&A
- `open_questions/` — unresolved questions (`page_type` **question** in plans)
- `synthesis/` — e.g. current-state
- `indexes/` — `index.md` (navigation) and `ingest-log.md` (changelog; `page_type` **changelog**)

**Private scope** (`wiki/users/<owner>/`):

- `sources/`, `concepts/`, `faq/`, `open_questions/`, `synthesis/`, `indexes/` (same roles; no `entities/` root in seeded private layout)

**Raw** mirrors scope: `raw/shared/canonical/<source_id>/`, `raw/users/<owner>/canonical/<source_id>/`.

**State:** `state/manifests/{shared|users/<owner>}/`, `state/runs/{shared|users/<owner>}/`, `state/promotion_logs/`.

**Exports:** `exports/diffs/{shared|users/<owner>}/<job_id>.patch`.
"""
