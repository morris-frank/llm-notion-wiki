from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import threading
from typing import Any
from urllib import error, parse

from .config import Settings
from .llm import OpenAICompatiblePlanner
from .logging_utils import configure_logging, log_event
from .notion import NotionAPIError, NotionClient
from .paths import ScopedPaths
from .repository import NotionRepository
from .sources import SourceFetcher
from .worker import Worker


LOGGER = logging.getLogger(__name__)


def build_worker(settings: Settings) -> Worker:
    planner = None
    if settings.llm_api_key and settings.llm_model:
        planner = OpenAICompatiblePlanner(
            api_key=settings.llm_api_key,
            api_base=settings.llm_api_base,
            model=settings.llm_model,
            system_prompt="Return valid JSON only. Follow the maintainer contract and file-operation contract exactly.",
        )
    client = NotionClient(
        token=settings.notion_token,
        version=settings.notion_version,
        api_base=settings.notion_api_base,
    )
    repository = NotionRepository(
        client=client,
        sources_data_source_id=settings.sources_data_source_id,
        wiki_data_source_id=settings.wiki_data_source_id,
        jobs_data_source_id=settings.jobs_data_source_id,
        policies_data_source_id=settings.policies_data_source_id,
    )
    source_fetcher = SourceFetcher(client, settings.wiki_root)
    return Worker(
        repository=repository,
        source_fetcher=source_fetcher,
        planner=planner,
        wiki_root=settings.wiki_root,
        worker_name=settings.worker_name,
    )


@dataclass
class ServiceApp:
    settings: Settings
    worker: Worker

    def enqueue_source(self, source_page_id: str) -> dict[str, Any]:
        job = self.worker.enqueue_ingest_job(source_page_id)
        return {
            "job_id": job.job_id,
            "job_page_id": job.page_id,
            "status": job.status,
            "target_source_page_id": job.target_source_page_id,
            "scope": job.scope,
            "owner": job.owner,
        }

    def inspect_jobs(self, status: str | None) -> dict[str, Any]:
        jobs = self.worker.repository.query_jobs(status=status, page_size=20)
        return {
            "jobs": [
                {
                    "job_id": job.job_id,
                    "page_id": job.page_id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "scope": job.scope,
                    "owner": job.owner,
                    "target_source_page_id": job.target_source_page_id,
                    "idempotency_key": job.idempotency_key,
                }
                for job in jobs
            ]
        }

    def requeue_job(self, job_page_id: str) -> dict[str, Any]:
        job = self.worker.repository.requeue_job(job_page_id)
        log_event(LOGGER, "job_requeued", job_id=job.job_id, page_id=job.page_id, scope=job.scope, owner=job.owner)
        return {
            "job_id": job.job_id,
            "page_id": job.page_id,
            "status": job.status,
            "scope": job.scope,
            "owner": job.owner,
        }

    def _signed(self, raw_body: bytes, signature: str | None) -> bool:
        secret = self.settings.notion_webhook_signing_secret or self.settings.notion_webhook_verification_token
        if not secret or not signature:
            return False
        presented = signature.strip()
        if presented.startswith("sha256="):
            presented = presented.split("=", 1)[1]
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(presented, expected)

    def handle_webhook(self, raw_body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        payload = json.loads(raw_body.decode("utf-8"))
        log_event(LOGGER, "webhook_received", payload_type=payload.get("type"))
        verification_token = payload.get("verification_token")
        configured_token = self.settings.notion_webhook_verification_token
        if verification_token:
            if configured_token and verification_token != configured_token:
                return HTTPStatus.FORBIDDEN, {"error": "verification token mismatch"}
            return HTTPStatus.OK, {"ok": True}
        if not (self.settings.notion_webhook_signing_secret or configured_token):
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "NOTION_WEBHOOK_SIGNING_SECRET or NOTION_WEBHOOK_VERIFICATION_TOKEN is not configured"
            }
        signature = headers.get("X-Notion-Signature")
        if not self._signed(raw_body, signature):
            return HTTPStatus.UNAUTHORIZED, {"error": "invalid signature"}
        event_type = payload.get("type") or "unknown"
        entity = payload.get("entity") or {}
        if entity.get("type") != "page" or not entity.get("id"):
            return HTTPStatus.OK, {"accepted": False, "reason": "event entity is not a page"}
        try:
            source = self.worker.repository.get_source(entity["id"])
        except (NotionAPIError, error.HTTPError):
            return HTTPStatus.OK, {"accepted": False, "reason": "page is not a readable source row"}
        if source.properties.get("Source Title") is None:
            return HTTPStatus.OK, {"accepted": False, "reason": "page is not a source row"}
        event_class = "trigger_regeneration" if source.trigger_regeneration else "source_update"
        suffix = source.checksum or source.last_edited_time or str(source.content_version or 0)
        job_type = "update_wiki" if source.trigger_regeneration else "ingest_source"
        title_prefix = "Regenerate wiki from" if source.trigger_regeneration else "Ingest"
        key = f"{source.source_id}:{source.scope}:{source.owner or '-'}:{event_class}:{suffix}"
        job = self.worker.repository.create_job(
            job_type=job_type,
            title=f"{title_prefix} {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=key,
            scope_context=source.scope_context,
            policy_page_id=self.worker.repository.active_policy_page_id(source.scope_context),
        )
        log_event(
            LOGGER,
            "webhook_job_created",
            event_type=event_type,
            event_class=event_class,
            job_id=job.job_id,
            scope=job.scope,
            owner=job.owner,
        )
        return HTTPStatus.OK, {"accepted": True, "job_id": job.job_id, "event_type": event_type, "event_class": event_class}


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class LLMWikiRequestHandler(BaseHTTPRequestHandler):
    server: "LLMWikiHTTPServer"

    def _admin_authorized(self) -> bool:
        if not self.server.app.settings.admin_api_key:
            return True
        return self.headers.get("X-Admin-Key") == self.server.app.settings.admin_api_key

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            _json_response(self, HTTPStatus.OK, {"status": "ok"})
            return
        if self.path.startswith("/admin/jobs"):
            if not self._admin_authorized():
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "invalid admin key"})
                return
            parsed = parse.urlparse(self.path)
            status = parse.parse_qs(parsed.query).get("status", [None])[0]
            _json_response(self, HTTPStatus.OK, self.server.app.inspect_jobs(status))
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        if self.path == "/notion/webhook":
            status, payload = self.server.app.handle_webhook(raw_body, dict(self.headers.items()))
            _json_response(self, status, payload)
            return
        if self.path == "/admin/enqueue/source":
            if not self._admin_authorized():
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "invalid admin key"})
                return
            body = json.loads(raw_body.decode("utf-8"))
            source_page_id = body.get("source_page_id")
            if not source_page_id:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "source_page_id is required"})
                return
            payload = self.server.app.enqueue_source(source_page_id)
            _json_response(self, HTTPStatus.ACCEPTED, payload)
            return
        if self.path == "/admin/requeue/job":
            if not self._admin_authorized():
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "invalid admin key"})
                return
            body = json.loads(raw_body.decode("utf-8"))
            job_page_id = body.get("job_page_id")
            if not job_page_id:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "job_page_id is required"})
                return
            _json_response(self, HTTPStatus.ACCEPTED, self.server.app.requeue_job(job_page_id))
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        return


class LLMWikiHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app: ServiceApp) -> None:
        super().__init__(server_address, LLMWikiRequestHandler)
        self.app = app


def serve(settings: Settings, host: str, port: int) -> None:
    configure_logging(settings.log_level)
    app = ServiceApp(settings=settings, worker=build_worker(settings))
    stop_event = threading.Event()

    def worker_loop() -> None:
        while not stop_event.is_set():
            try:
                job = app.worker.run_once()
                if job:
                    scoped_paths = ScopedPaths(app.worker.wiki_root, job.scope_context)
                    log_event(
                        LOGGER,
                        "worker_iteration",
                        job_id=job.job_id,
                        scope=job.scope,
                        owner=job.owner,
                        wiki_scope_root=scoped_paths.relative(scoped_paths.wiki_scope_root),
                    )
            except Exception as exc:  # pragma: no cover - service loop safety
                LOGGER.exception("worker loop failure")
                log_event(LOGGER, "worker_loop_exception", message=str(exc))
            stop_event.wait(settings.poll_interval_seconds)

    thread = threading.Thread(target=worker_loop, name="llmwiki-worker", daemon=True)
    thread.start()
    server = LLMWikiHTTPServer((host, port), app)
    log_event(LOGGER, "service_started", host=host, port=port, wiki_root=str(settings.wiki_root))
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        thread.join(timeout=1)
