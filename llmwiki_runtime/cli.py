from __future__ import annotations

import argparse

from .config import Settings
from .service import build_worker, serve
from .wiki_ops import ensure_wiki_root


def main() -> None:
    parser = argparse.ArgumentParser(prog="llmwiki-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)

    enqueue_parser = subparsers.add_parser("enqueue-source")
    enqueue_parser.add_argument("source_page_id")

    subparsers.add_parser("run-once")
    subparsers.add_parser("init-wiki-root")

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "serve":
        serve(settings, args.host, args.port)
        return

    if args.command == "init-wiki-root":
        ensure_wiki_root(settings.wiki_root)
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


if __name__ == "__main__":
    main()
