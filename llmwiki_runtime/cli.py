from __future__ import annotations

import argparse
import json

from .config import Settings
from .logging_utils import configure_logging
from .service import build_worker, serve
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
