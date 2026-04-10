# Code layout

Single package: **`llmwiki_runtime`** (console entry: `llmwiki-runtime` → `cli.py`).

| Module | Responsibility |
|--------|----------------|
| `config.py` | `Settings.from_env()` — required Notion DS IDs, paths, LLM and webhook options. |
| `cli.py` | Argparse subcommands; wires settings, worker, `ServiceApp`, live verification. |
| `service.py` | `build_worker()`, `ServiceApp` (HTTP helpers, webhook handling, job enqueue), `LLMWikiHTTPServer` + `serve()` (worker thread + server). |
| `worker.py` | `Worker` — job execution: ingest → `update_wiki`, LLM plan → `wiki_ops`, Notion sync, questions, promotions. |
| `repository.py` | `NotionRepository` — maps Notion pages ↔ records; jobs CRUD, policies, wiki/entity/question/promotion updates. |
| `notion.py` | `NotionClient` (stdlib `urllib`), property builders, page markdown export. |
| `models.py` | Dataclasses: `ScopeContext`, sources, jobs, policies, `RunPlan`, etc. |
| `wiki_ops.py` | Parse/validate LLM run plans, apply file ops, manifests, diffs, atomic writes. |
| `contracts.py` | Maintainer prompts + JSON file-operation contract text. |
| `sources.py` | `SourceFetcher` — web pages and Notion pages → artifacts under scoped `raw/`. |
| `paths.py` | `ScopedPaths` — canonical paths for shared vs private wiki/raw/state. |
| `frontmatter.py` | Markdown frontmatter parse/serialize. |
| `llm.py` | `OpenAICompatiblePlanner` (`/chat/completions`), `Planner` protocol, `StaticPlanner` for tests. |
| `logging_utils.py` | Structured-ish logging helpers. |
| `live_verify.py` | Creates Notion rows and drives worker for integration scenarios; writes reports under `state/live_verification/`. |

**Tests** live under `tests/`; there is no `tests` package on the path beyond unittest discovery.

**Repo scripts** (shell): `bootstrap_llmwiki_notion_dynamic.sh`, `verify_llmwiki_notion_dynamic.sh`, `llmwiki_notion_setup.sh` — database bootstrap and verification; they use `CONTROL_DB_ID`, which the **Python runtime loads but does not use** (only the shell tooling references the control database).

**Canonical specs** (versioned under `docs/`): [`docs/shared.md`](docs/shared.md) (layout + scopes), [`docs/interface.md`](docs/interface.md) (JSON plans), [`docs/wp3-worker-algo.md`](docs/wp3-worker-algo.md) (worker algorithm). [`docs/wp3.md`](docs/wp3.md) is a deprecated flat-layout sketch. See [`REQUIREMENTS_COVERAGE.md`](REQUIREMENTS_COVERAGE.md).

---

## Data flow (high level)

1. **Jobs** are rows in the Jobs data source. `NotionRepository.query_queued_jobs` returns work; `claim_job` sets status/lock.
2. **`ingest_source`**: `SourceFetcher.fetch` writes `source.md` / metadata under `ScopedPaths.source_artifact_dir`; Notion source row updated; a dependent **`update_wiki`** job is enqueued.
3. **`update_wiki` / `answer_question` / `promote_private`**: Bundle is built → `Planner.plan` (JSON plan) → `wiki_ops` validates → if `run_mode` is **`apply`**, writes files, manifest, diff, and `NotionRepository` upserts wiki rows (and entities, etc.); if **`dry_run`**, writes a run record with `"dry_run": true` and skips wiki disk writes, manifest, diff, and wiki/source Notion upserts (job row may still be marked succeeded with the run record URI).
4. **Webhooks** (`service.handle_webhook`): optional verification handshake; signed deliveries create jobs via the same `create_job` path as the CLI.

---

## Incomplete or unused pieces

- **`control_db_id` in `Settings`**: populated from `CONTROL_DB_ID` but unused by Python; meant for bootstrap/verify scripts.
- **Strict dependency list**: `pyproject.toml` has `dependencies = []` — everything is stdlib; no pinned third-party stack (by design, but worth knowing for supply-chain tooling).

---

## Code quality, risks, and sharp edges

**Security**

- **`ADMIN_API_KEY` unset**: `_admin_authorized()` treats all admin requests as allowed. Set a key in any exposed deployment.
- **Webhook crypto**: `_signed` may use either `NOTION_WEBHOOK_SIGNING_SECRET` or, if the former is missing, `NOTION_WEBHOOK_VERIFICATION_TOKEN` as the HMAC key. Notion’s verification token is primarily for the subscription handshake; prefer the **signing secret** for `X-Notion-Signature` and keep semantics aligned with Notion’s docs.
- **SSRF / arbitrary fetch**: `SourceFetcher` requests URLs from Notion-defined sources (`web_page`). Compromise or misuse of Notion rows could point fetches at internal URLs — mitigate with network policy or URL allowlists if needed.
- **Secrets in files**: `env.local` is gitignored; never commit tokens. Rotate anything that ever leaked into a repo or chat.

**Operations**

- **HTTP server**: `ThreadingHTTPServer` only — no TLS, no auth on webhook path beyond signature/token checks. Put behind a reverse proxy with TLS in production.
- **Multi-worker**: Job claiming is “update page then re-read”; two processes may race; Notion does not give you a DB-style serializable transaction. Expect rare double-processing or lost claims under contention.
- **Large payloads**: `Content-Length` is trusted for reads; absurd values could cause memory pressure.

**Design / maintainability**

- **`__code__.co_varnames` introspection** in `ServiceApp._create_job` and `Worker._create_job` / `_update_source_after_wiki` / `_upsert_wiki_page`: backward-compatibility shim for tests or alternate repository implementations — brittle and easy to break when renaming parameters.
- **`Worker.run_job`**: Unknown exceptions are marked failed then **re-raised** — logged by the service loop; job state is still “failed,” but the exception propagates (intentional for visibility, noisy in logs).
- **Private repository methods from `ServiceApp`**: e.g. `_source_from_page`, `_question_from_page` — webhook path reaches into “private” helpers; coupling between layers is tight.

**Testing gaps**

- Several `pragma: no cover` branches (service loop, generic exception paths).

---

## Dependency policy

Runtime is **stdlib-only** (`urllib`, `html.parser`, etc.). Tests may use additional patterns; there is no `requirements-dev.txt` in the tree from this overview—use `pip install -e .` and run `python -m unittest` or your CI’s test command as configured.
