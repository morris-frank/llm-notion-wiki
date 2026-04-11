# Scoped LLM Wiki (team shared + private)

This document is the **canonical on-disk layout** for `llmwiki-runtime`. It matches [llmwiki_runtime/paths.py](../llmwiki_runtime/paths.py) (`scope_root_directories`, `ScopedPaths`). Older single-tree sketches (flat `wiki/` at repo root) are non-normative; see [wp3.md](wp3.md).

## 1. Canonical structure

Under `WIKI_ROOT` (often `./llmwiki` or an absolute path):

```
llmwiki/
  AGENTS.shared.md
  AGENTS.private.template.md
  config/
    file-operation-contract.md
  schema/
    shared.md
    private.md
    promotion.md
    taxonomy.md

  raw/
    shared/
      inbox/
      canonical/
        <source_id>/
          metadata.json
          source.txt
          source.md
      archive/
    users/
      <owner>/
        inbox/
        canonical/
          <source_id>/
            ...
        archive/

  wiki/
    shared/
      sources/
      concepts/
      entities/
      faq/
      open_questions/
      synthesis/
      indexes/
        index.md
        ingest-log.md
    users/
      <owner>/
        sources/
        concepts/
        faq/
        open_questions/
        synthesis/
        indexes/
          index.md
          ingest-log.md

  state/
    manifests/
      shared/
      users/<owner>/
    runs/
      shared/
      users/<owner>/
    promotion_logs/
    webhook/

  reviews/
    promotion_queue/
    rejected/
    approved/

  exports/
    diffs/
      shared/
      users/<owner>/
```

**Notes**

- **Index and changelog** both live under `wiki/<scope>/indexes/` (`index.md`, `ingest-log.md`). The runtime uses `page_type` `index` vs `changelog` with path rules in `paths.py`.
- `**state/`** may also contain operational files (e.g. live verification reports) not listed above.
- Optional future directories such as `ingestion_logs/` or `locks/` are not created by the current runtime; do not assume they exist.

## 2. Scope model

Use exactly three *scopes* (see `ScopeContext` in code):

1. **shared** — organisational knowledge (team-visible).
2. **private** — user-specific knowledge; requires an **owner** string (Unix-safe segment).
3. **promoted** — not a storage scope; a workflow state meaning “private material approved for copying into shared”.

Do not let `wiki/shared/` depend on private sources unless content has passed promotion. Otherwise provenance and access control are unsound.

## 3. Query semantics

When *answering* from the wiki (not implemented as an API here; policy for agents):

- **Personal query by Alice:** `wiki/users/<alice>/` first, then `wiki/shared/`.
- **Team query:** `wiki/shared/` only.

Never treat a team-visible answer as sourced only from `wiki/users/<user>/`.

## 4. Ingestion rules

### 4.1 Shared ingestion

- **Write raw artefacts:** `raw/shared/canonical/<source_id>/`.
- **Compiled wiki:** `wiki/shared/...` (via worker / LLM plans).

### 4.2 Private ingestion

- **Write raw artefacts:** `raw/users/<owner>/canonical/<source_id>/`.
- **Compiled wiki:** `wiki/users/<owner>/...`.

### 4.3 Promotion

- **Queue / review:** `reviews/promotion_queue/`, plus Notion **Promotions** data source when configured.
- **After approval:** updates under `wiki/shared/...`, logs under `state/promotion_logs/`, and Notion rows updated by `promote_private` jobs.

## 5. Minimal metadata (frontmatter)

Compiled wiki pages use YAML frontmatter validated by the worker (`wiki_ops`, `frontmatter`). Typical fields include:

- `title`, `page_type`, `slug`, `status`, `updated_at`
- `source_ids` — list of **source ID strings** (e.g. `src_001`), not filesystem paths to raw files
- `source_scope` — list including `shared` and/or `private` as appropriate
- `entity_keys`, `concept_keys`
- `confidence`, `review_required`
- `scope` — `shared` or `private`
- `owner` — `null` or string for private scope
- `review_state`, `promotion_origin`

**Example (shared concept page):**

```yaml
---
title: "Example concept"
page_type: concept
slug: example-concept
status: draft
updated_at: "2026-04-09T14:00:00Z"
source_ids:
  - "src_ops_sample"
source_scope:
  - shared
entity_keys: []
concept_keys: []
confidence: medium
review_required: false
scope: shared
owner: null
review_state: unreviewed
promotion_origin: null
---
```

**Example (private page):**

```yaml
---
title: "Private note"
page_type: concept
slug: private-note
status: draft
updated_at: "2026-04-09T14:00:00Z"
source_ids:
  - "src_alice_notes_001"
source_scope:
  - private
scope: private
owner: alice
review_state: n_a
promotion_origin: null
---
```

## 6. Folder meanings (aligned with the runtime)

Writable wiki roots per scope are:


| Area              | Purpose                                                                                         |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `sources/`        | One summary page per source ID (`<source_id>.md`)                                               |
| `concepts/`       | Stable definitions and narrative concept pages                                                  |
| `entities/`       | **Shared scope only** in the current runtime — canonical entity pages                           |
| `faq/`            | Answered Q&A (durable FAQ entries)                                                              |
| `open_questions/` | Unresolved questions (`page_type` **question** in JSON plans)                                   |
| `synthesis/`      | e.g. `current-state.md`                                                                         |
| `indexes/`        | Navigation (`index.md`) and append-only ingest log (`ingest-log.md`, `page_type` **changelog**) |


Decisions, projects, procedures, or private `notes/` trees are **not** separate top-level directories in the current implementation; model that content as **concepts** or other **allowed page types** under the paths above.

## 7. Maintainer policy

1. Raw trees under `raw/` are immutable for the maintainer; they are the source of truth.
2. `wiki/` holds compiled artefacts.
3. Shared pages may only cite shared (or promoted) sources.
4. Private pages may cite private and shared sources for the same owner.
5. Contradictions are surfaced explicitly, not silently merged.
6. Promotion copies or rewrites into shared; it does not expose the original private page by reference alone.

## 8. Promotion workflow (implementation)

1. Private raw → private compiled page (normal ingest / update jobs).
2. Promotion row in Notion (approved) + optional candidate JSON under `reviews/promotion_queue/`.
3. `promote_private` job applies LLM plan to `wiki/shared/...`.
4. Logs: `state/promotion_logs/<promotion_id>.json`, Notion promotion + wiki rows updated.

Example paths compatible with the runtime (concept pages under `concepts/`):

```text
source_page: wiki/users/alice/concepts/reporting-friction.md
target_pages:
  - wiki/shared/concepts/customer-reporting.md
```

## 9. Recommended schema files

Seeded under `schema/` (see [llmwiki_runtime/contracts.py](../llmwiki_runtime/contracts.py)):

- **shared.md** — shared provenance and citation rules.
- **private.md** — private scope rules.
- **promotion.md** — promotion workflow and constraints.
- **taxonomy.md** — folder taxonomy matching `paths.py`.

## 10. Default naming conventions

```text
raw/shared/canonical/meetings/2026-04-09-management-call.md
raw/users/alice/canonical/notes/2026-04-09-customer-call.md

wiki/shared/concepts/ddr-001-report-generation.md
wiki/shared/concepts/customer-reporting.md
wiki/users/alice/open_questions/reporting-ux.md
```

## 11. Best default operating model

- One shared base wiki (`wiki/shared/`).
- One private overlay per user (`wiki/users/<owner>/`).
- One explicit promotion pipeline (Notion + `reviews/` + `promote_private` jobs).

## 12. Maintainer prompt summary

The runtime injects scoped maintainer contracts from `contracts.maintainer_contract` and the file-operation contract. Treat `raw/` as source of truth, `wiki/` as compiled output, and respect scope boundaries in JSON plans.

## 13. Caveats

- Multi-scope design is a disciplined extension of a single-tree wiki pattern; folder names here follow **this runtime**, not every historical doc draft.
- ACLs must not rely on prompts alone; storage and deployment boundaries matter.

## 14. Recommendation

Adopt strict folder-level scope separation, shared base + private overlays, explicit promotion, provenance in frontmatter, and no direct private-to-shared dependency without promotion.