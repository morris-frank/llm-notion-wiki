# Requirements coverage vs `docs/` (scoped layout)

This document compares **llmwiki-runtime** to the specifications in [`docs/`](docs/). The **canonical** on-disk and scope model is **[`docs/shared.md`](docs/shared.md)** plus [`llmwiki_runtime/paths.py`](llmwiki_runtime/paths.py).

**Non-canonical:** [`docs/wp3.md`](docs/wp3.md) (flat single-tree layout) is deprecated in favour of scoped paths; it remains as historical / Karpathy-reference text only.

| Document | Role | Coverage notes |
|----------|------|----------------|
| [`docs/shared.md`](docs/shared.md) | Scoped `wiki/shared`, `wiki/users/<owner>`, raw trees, promotion | Aligned to `paths.py` and seeded `schema/*.md` text in `contracts.py`. |
| [`docs/interface.md`](docs/interface.md) | JSON run envelope + operations | Scoped path examples; `page_type` includes `question` (not `comparison`). `dry_run` documented: no wiki disk writes, manifest, diff, or wiki/source Notion upserts; job row may still succeed with run record URI. |
| [`docs/wp3-worker-algo.md`](docs/wp3-worker-algo.md) | Phases, validation, manifests, diffs | Includes a **Paths** subsection for scoped `state/`, `exports/`, `raw/`. |
| [`docs/wp1-3.md`](docs/wp1-3.md) | WP1–WP3 | WP2 footnote: ingest uses **parsed** then **`update_wiki`**; **processed** after wiki; paths per `shared.md`. |
| [`docs/wp3.md`](docs/wp3.md) | Flat layout | Deprecated; see banner at top of file. |

---

## Implemented vs remaining gaps

| Topic | Status |
|-------|--------|
| Scoped directory layout | **Done** — see `shared.md` + `paths.py`. |
| `dry_run` run mode | **Done** — [`worker.py`](llmwiki_runtime/worker.py) `_run_planned_wiki_job` skips `atomic_write_files`, manifest, diff, `_sync_wiki_pages`, `_update_source_after_wiki`; writes run record with `"dry_run": true`. |
| `FEATURE_JOB_CONTROL` in Python | **Not implemented** — bootstrap/shell only; optional future. |
| Webhook append-only audit log | **Not implemented** — still `last_delivery.json` / `last_verification.json` only. |
| `jsonschema` library | **Not used** — validation is hand-coded in `wiki_ops.py`. |
| `model_failure` error class | **Partial** — still mapped largely to `validation` / `unknown`. |

---

## Rough overall coverage

Relative to **scoped** docs + [`interface.md`](docs/interface.md) + [`wp3-worker-algo.md`](docs/wp3-worker-algo.md), the core loop (webhook → job → ingest → `update_wiki` → Notion) is **largely complete**, with explicit **`dry_run`** behaviour and documentation for side effects.
