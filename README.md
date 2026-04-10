# llmwiki-runtime

A small Python service that keeps a **local markdown wiki** in sync with **Notion control-plane databases** (sources, jobs, wiki pages, policies, optional questions/promotions/entities). It polls the Jobs data source, runs a worker loop, and can accept Notion webhooks and admin HTTP calls.

## Requirements

- Python **3.11+**
- A Notion integration with access to the configured **data sources** (Notion API uses data source IDs).
- Optional: **OpenAI-compatible** API (`OPENAI_API_KEY` + `OPENAI_MODEL`) for `update_wiki`, `answer_question`, and `promote_private` jobs. Without LLM config, `ingest_source` still runs (fetch → markdown), but wiki-update jobs fail until a planner is configured.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

Set environment variables (e.g. in a sourced shell file you keep out of git). Minimum required by `Settings.from_env()`:

| Variable | Purpose |
|----------|---------|
| `NOTION_TOKEN` | Notion integration secret |
| `SOURCES_DS_ID`, `WIKI_DS_ID`, `JOBS_DS_ID`, `POLICIES_DS_ID` | Data source IDs for each control-plane table |
| `WIKI_ROOT` | Root directory for raw sources, `wiki/`, `state/`, etc. (default `./llmwiki`) |

Optional: `NOTION_VERSION`, `NOTION_API_BASE`, `ENTITIES_DS_ID`, `QUESTIONS_DS_ID`, `PROMOTIONS_DS_ID`, `OPENAI_*` / `LLM_*`, `ADMIN_API_KEY`, `PUBLIC_BASE_URL`, webhook secrets, `POLL_INTERVAL_SECONDS`, `LOG_LEVEL`.

Load vars before running, e.g. `set -a && source env.local && set +a`.

## Run the HTTP server + worker

```bash
llmwiki-runtime serve --host 0.0.0.0 --port 8000
```

- **Worker**: Background thread polls queued jobs and runs one job per iteration (`POLL_INTERVAL_SECONDS` between attempts).
- **Health**: `GET /healthz`
- **Webhook**: `POST /notion/webhook` — configure in Notion with `PUBLIC_BASE_URL` + signing secret / verification token.
- **Webhook status**: `GET /notion/webhook/status`
- **Admin** (requires `X-Admin-Key` if `ADMIN_API_KEY` is set): `GET /admin/jobs`, `POST /admin/enqueue/source`, `POST /admin/requeue/job`

## CLI (same binary)

| Command | Role |
|---------|------|
| `init-wiki-root [--owner NAME]` | Create wiki layout under `WIKI_ROOT` |
| `enqueue-source <source_page_id>` | Queue an ingest job |
| `run-once` | Process one queued job (for debugging) |
| `inspect-jobs [--status …]` | List recent jobs |
| `requeue-job <job_page_id>` | Reset a job to queued |
| `webhook doctor` | Print webhook setup checklist |
| `webhook verify --payload-file … --signature …` | Check signature locally |
| `verify-live [--scenario …] [--cleanup-mode …]` | End-to-end tests against live Notion (creates data) |

## Wiki layout

Under `WIKI_ROOT`, the app expects scoped trees such as `wiki/shared/`, `wiki/users/<owner>/`, `raw/…`, `state/manifests/`, run records, and diffs. See `CODEBASE.md` for module mapping.

## Documentation (canonical specs)

| Doc | Content |
|-----|---------|
| [docs/shared.md](docs/shared.md) | Scoped filesystem layout and scope model (matches the runtime). |
| [docs/interface.md](docs/interface.md) | LLM JSON run envelope and file operations (`dry_run`, `page_type`, paths). |
| [docs/wp3-worker-algo.md](docs/wp3-worker-algo.md) | Worker phases and scoped artifact paths. |
| [docs/wp1-3.md](docs/wp1-3.md) | Work packages WP1–WP3 (read with `shared.md` paths). |
| [REQUIREMENTS_COVERAGE.md](REQUIREMENTS_COVERAGE.md) | Traceability vs these docs. |

Historical flat-layout notes: [docs/wp3.md](docs/wp3.md) (deprecated for this runtime).
