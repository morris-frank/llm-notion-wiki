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


| Variable                                                      | Purpose                                                                       |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `NOTION_TOKEN`                                                | Notion integration secret                                                     |
| `SOURCES_DS_ID`, `WIKI_DS_ID`, `JOBS_DS_ID`, `POLICIES_DS_ID` | Data source IDs for each control-plane table                                  |
| `WIKI_ROOT`                                                   | Root directory for raw sources, `wiki/`, `state/`, etc. (default `./llmwiki`) |


Optional: `NOTION_VERSION`, `NOTION_API_BASE`, `ENTITIES_DS_ID`, `QUESTIONS_DS_ID`, `PROMOTIONS_DS_ID`, `OPENAI_*` / `LLM_*`, `ADMIN_API_KEY`, `LLMWIKI_INSECURE_ADMIN` (see below), `PUBLIC_BASE_URL`, webhook secrets, `POLL_INTERVAL_SECONDS`, `LOG_LEVEL`.

Load vars before running, e.g. `set -a && source env.local && set +a`.

For a full production-style setup, see **[Hosted deployment](#hosted-deployment)** and **[Notion integration](#notion-integration-tokens-and-webhooks)** below.

## Hosted deployment

The runtime is a single Python process (`llmwiki-runtime serve`) plus a writable `WIKI_ROOT`. You need **HTTPS** on the public URL Notion will call for webhooks, and a stable disk (or mounted volume) for the wiki and `state/`.

### VPS (e.g. Hetzner Cloud) — simple individual or small-team host

Typical pattern: one small Ubuntu VM, you install the app and put a reverse proxy in front.

1. **VM**: Create an instance (e.g. Ubuntu 24.04), note its public IPv4, point a DNS **A record** (e.g. `wiki.example.com`) at it.
2. **Firewall**: Allow SSH (22) and HTTP/HTTPS (80/443) only; deny the app port from the internet if the app listens on localhost (recommended).
3. **System packages**: Install `git`, Python **3.11+**, and a TLS-capable reverse proxy (**Caddy** or **nginx**). Create a system user for the app if you like.
4. **App install**: Clone this repo, create a venv, `pip install -e .`, run `llmwiki-runtime init-wiki-root` once so `WIKI_ROOT` has the expected layout (set `WIKI_ROOT` to a persistent path, e.g. `/var/lib/llmwiki`).
5. **Secrets on disk**: Put environment variables in a root-only file (e.g. `/etc/llmwiki/env`, mode `600`) — never commit it. Include at least `NOTION_TOKEN`, data source IDs, `WIKI_ROOT`, `ADMIN_API_KEY` (e.g. `openssl rand -hex 32`), `PUBLIC_BASE_URL=https://wiki.example.com`, and webhook variables once Notion is configured (below).
6. **Bind locally**: Run `llmwiki-runtime serve --host 127.0.0.1 --port 8000` so only the proxy talks to the app; set `ADMIN_API_KEY` so admin routes require `X-Admin-Key` (loopback bind allows omitting the key only for pure local dev — production should always set a key).
7. **Reverse proxy**: Terminate TLS at Caddy/nginx and `proxy_pass` to `http://127.0.0.1:8000`. Expose `/healthz`, `/notion/webhook`, and (if needed) `/admin/*` only through HTTPS.
8. **Process manager**: Use **systemd** (or similar) to run `serve` on boot, restart on failure, and load `EnvironmentFile=/etc/llmwiki/env`.

### Google Cloud — team-oriented deployment

Use GCP when you want shared secrets, IAM, and repeatable deploys.

1. **Compute Engine**: Treat a Linux VM like the Hetzner flow above; store env in **Secret Manager** or a protected file, and inject at startup via systemd or a small startup script. Use OS Login or SSH keys for access.
2. **Cloud Run** (optional): Package the app in a container, set env vars from **Secret Manager** references, and set `PUBLIC_BASE_URL` to your HTTPS service URL. Ensure the container has a writable volume or external storage if `WIKI_ROOT` must survive restarts (Cloud Run is ephemeral unless you attach storage).
3. **Secrets**: Store `NOTION_TOKEN`, `NOTION_WEBHOOK_VERIFICATION_TOKEN`, `NOTION_WEBHOOK_SIGNING_SECRET`, and `ADMIN_API_KEY` in Secret Manager; grant only the runtime service account access.
4. **HTTPS**: Front with **HTTPS Load Balancing** or Cloud Run’s managed HTTPS so `PUBLIC_BASE_URL` matches what Notion will call.

Pick one pattern; both Hetzner-style VPS and GCP VMs behave the same from the app’s perspective: HTTPS URL + env file or Secret Manager + systemd (or container orchestration).

## Notion integration, tokens, and webhooks

### 1. Create the integration and get `NOTION_TOKEN`

1. Open **[My integrations](https://www.notion.so/my-integrations)** → **New integration**.
2. Choose the workspace, set capabilities (read/write content as needed), create the integration.
3. Copy the **Internal Integration Secret** — this is `NOTION_TOKEN`. Keep it only in env or Secret Manager, never in git.

### 2. Connect databases to the integration

For each Notion **database** that backs a data source (sources, jobs, wiki, policies, etc.):

1. Open the database → **⋯** → **Connections** (or **Add connections**) → select your integration so the token can read and update rows.

Without this step, API calls return permission errors.

### 3. Data source IDs (`SOURCES_DS_ID`, `WIKI_DS_ID`, …)

This runtime is configured with **data source** IDs (Notion API “data source” objects), not only parent page IDs. If you used this repo’s bootstrap scripts (`bootstrap_llmwiki_notion_dynamic.sh`, etc.), follow those scripts to create tables and print IDs. Otherwise, obtain each ID from the Notion API or from your database URL / developer tools as appropriate for your workspace, and set:

- `SOURCES_DS_ID`, `WIKI_DS_ID`, `JOBS_DS_ID`, `POLICIES_DS_ID` (required)
- Optional: `ENTITIES_DS_ID`, `QUESTIONS_DS_ID`, `PROMOTIONS_DS_ID` if you use those features

### 4. Public base URL (`PUBLIC_BASE_URL`)

Set `PUBLIC_BASE_URL` to the **origin** Notion will use to reach your server, with **https** and **no path**, e.g. `https://wiki.example.com`. This must match how you terminate TLS (reverse proxy or load balancer). Webhook delivery requires a URL that Notion can reach over the public internet with valid HTTPS.

### 5. Webhook verification and signing secrets

1. Deploy the service so `GET https://<your-domain>/healthz` works.
2. In the integration’s **Webhook** settings in Notion, set the webhook URL to:

   `https://<your-domain>/notion/webhook`

3. Notion will send a **subscription handshake** payload containing `verification_token`. Copy that value into **`NOTION_WEBHOOK_VERIFICATION_TOKEN`** on the server (the server rejects handshake requests if this env var is unset).
4. Copy the **signing secret** for `X-Notion-Signature` into **`NOTION_WEBHOOK_SIGNING_SECRET`**. Do not use the verification token as the HMAC key.
5. On the server, run `llmwiki-runtime webhook doctor` with the same env loaded to confirm `endpoint`, signing secret, and verification token flags.
6. In Notion, subscribe to the relevant **page** events for your control-plane databases (per Notion’s webhook UI).

You can sanity-check a signature locally with `llmwiki-runtime webhook verify --payload-file … --signature …`.

### 6. Admin API key (`ADMIN_API_KEY`)

Generate a long random value (e.g. `openssl rand -hex 32`) and set **`ADMIN_API_KEY`**. Clients must send header **`X-Admin-Key`** on `/admin/*` routes. For production, always set this; do not rely on **`LLMWIKI_INSECURE_ADMIN`** except for local development.

### 7. Optional: LLM for wiki updates / questions / promotions

Set **`OPENAI_API_KEY`** and **`OPENAI_MODEL`** (or `LLM_*` equivalents) and an OpenAI-compatible **`OPENAI_BASE_URL`** if needed. Without these, `ingest_source` still runs; `update_wiki`, `answer_question`, and `promote_private` need a configured planner.

## Run the HTTP server + worker

```bash
llmwiki-runtime serve --host 0.0.0.0 --port 8000
```

- **Worker**: Background thread polls queued jobs and runs one job per iteration (`POLL_INTERVAL_SECONDS` between attempts).
- **Health**: `GET /healthz`
- **Webhook**: `POST /notion/webhook` — set `NOTION_WEBHOOK_VERIFICATION_TOKEN` for Notion’s subscription handshake (handshake requests are rejected if unset), and `NOTION_WEBHOOK_SIGNING_SECRET` for `X-Notion-Signature` on event deliveries (do not use the verification token as the HMAC key).
- **Webhook status**: `GET /notion/webhook/status`
- **Admin**: `GET /admin/jobs`, `POST /admin/enqueue/source`, `POST /admin/requeue/job` — send header `X-Admin-Key` when `ADMIN_API_KEY` is set. If `ADMIN_API_KEY` is unset, `/admin/*` is unauthenticated: `serve` refuses to bind to non-loopback addresses unless you set `LLMWIKI_INSECURE_ADMIN=1` (local dev only).

## CLI (same binary)


| Command                                         | Role                                                |
| ----------------------------------------------- | --------------------------------------------------- |
| `init-wiki-root [--owner NAME]`                 | Create wiki layout under `WIKI_ROOT`                |
| `enqueue-source <source_page_id>`               | Queue an ingest job                                 |
| `run-once`                                      | Process one queued job (for debugging)              |
| `inspect-jobs [--status …]`                     | List recent jobs                                    |
| `requeue-job <job_page_id>`                     | Reset a job to queued                               |
| `webhook doctor`                                | Print webhook setup checklist                       |
| `webhook verify --payload-file … --signature …` | Check signature locally                             |
| `verify-live [--scenario …] [--cleanup-mode …]` | End-to-end tests against live Notion (creates data) |


## Wiki layout

Under `WIKI_ROOT`, the app expects scoped trees such as `wiki/shared/`, `wiki/users/<owner>/`, `raw/…`, `state/manifests/`, run records, and diffs. See `CODEBASE.md` for module mapping.

## Documentation (canonical specs)


| Doc                                                  | Content                                                                    |
| ---------------------------------------------------- | -------------------------------------------------------------------------- |
| This README (Hosted deployment, Notion…)            | VPS / GCP deployment and Notion tokens, data sources, webhooks, admin.    |
| [docs/shared.md](docs/shared.md)                     | Scoped filesystem layout and scope model (matches the runtime).            |
| [docs/interface.md](docs/interface.md)               | LLM JSON run envelope and file operations (`dry_run`, `page_type`, paths). |
| [docs/wp3-worker-algo.md](docs/wp3-worker-algo.md)   | Worker phases and scoped artifact paths.                                   |
| [docs/wp1-3.md](docs/wp1-3.md)                       | Work packages WP1–WP3 (read with `shared.md` paths).                       |
| [REQUIREMENTS_COVERAGE.md](REQUIREMENTS_COVERAGE.md) | Traceability vs these docs.                                                |


Historical flat-layout notes: [docs/wp3.md](docs/wp3.md) (deprecated for this runtime).

