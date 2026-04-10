from __future__ import annotations

import argparse
import json

from .config import Settings
from .live_verify import run_live_verification
from .logging_utils import configure_logging
from .service import ServiceApp, build_worker, serve
from .wiki_ops import ensure_owner_scope, ensure_wiki_root


def main() -> None:
    parser = argparse.ArgumentParser(prog="llmwiki-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)

    enqueue_parser = subparsers.add_parser("enqueue-source")
    enqueue_parser.add_argument("source_page_id")

    inspect_parser = subparsers.add_parser("inspect-jobs")
    inspect_parser.add_argument("--status", default=None)

    requeue_parser = subparsers.add_parser("requeue-job")
    requeue_parser.add_argument("job_page_id")

    init_parser = subparsers.add_parser("init-wiki-root")
    init_parser.add_argument("--owner", default=None)

    webhook_parser = subparsers.add_parser("webhook")
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_command", required=True)
    webhook_subparsers.add_parser("doctor")
    verify_parser = webhook_subparsers.add_parser("verify")
    verify_parser.add_argument("--payload-file", required=True)
    verify_parser.add_argument("--signature", required=True)

    live_verify_parser = subparsers.add_parser("verify-live")
    live_verify_parser.add_argument("--scenario", choices=["source", "question", "promotion", "webhook", "full"], default="full")
    live_verify_parser.add_argument("--cleanup-mode", choices=["keep", "archive", "purge"], default="keep")

    subparsers.add_parser("run-once")

    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if args.command == "serve":
        serve(settings, args.host, args.port)
        return

    if args.command == "init-wiki-root":
        ensure_wiki_root(settings.wiki_root)
        if args.owner:
            ensure_owner_scope(settings.wiki_root, args.owner)
        return

    if args.command == "webhook":
        app = ServiceApp(settings=settings, worker=build_worker(settings))
        if args.webhook_command == "doctor":
            status = app.webhook_status()
            endpoint = status["endpoint"] or "Set PUBLIC_BASE_URL to expose /notion/webhook"
            print(
                "\n".join(
                    [
                        f"Endpoint: {endpoint}",
                        f"Signing secret configured: {status['has_signing_secret']}",
                        f"Verification token configured: {status['has_verification_token']}",
                        "",
                        "Notion UI steps:",
                        "1. Open the integration webhook settings in Notion.",
                        "2. Paste the endpoint URL ending in /notion/webhook.",
                        "3. Copy the verification token into NOTION_WEBHOOK_VERIFICATION_TOKEN.",
                        "4. Copy the signing secret into NOTION_WEBHOOK_SIGNING_SECRET.",
                        "5. Subscribe to page events for the control-plane database pages.",
                    ]
                )
            )
            return
        raw_body = open(args.payload_file, "rb").read()
        print(json.dumps({"valid": app._signed(raw_body, args.signature)}, indent=2, sort_keys=True))
        return

    if args.command == "verify-live":
        print(json.dumps(run_live_verification(settings, scenario=args.scenario, cleanup_mode=args.cleanup_mode), indent=2, sort_keys=True))
        return

    worker = build_worker(settings)
    if args.command == "enqueue-source":
        job = worker.enqueue_ingest_job(args.source_page_id)
        print(job.job_id)
        return
    if args.command == "run-once":
        job = worker.run_once()
        if job:
            print(job.job_id)
        return
    if args.command == "inspect-jobs":
        jobs = worker.repository.query_jobs(status=args.status, page_size=20)
        print(
            json.dumps(
                [
                    {
                        "job_id": job.job_id,
                        "page_id": job.page_id,
                        "job_type": job.job_type,
                        "status": job.status,
                        "scope": job.scope,
                        "owner": job.owner,
                        "target_source_page_id": job.target_source_page_id,
                    }
                    for job in jobs
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.command == "requeue-job":
        job = worker.repository.requeue_job(args.job_page_id)
        print(job.job_id)
        return


if __name__ == "__main__":
    main()
