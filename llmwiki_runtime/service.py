from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
from pathlib import Path
import threading
from typing import Any
from urllib import parse

from .config import Settings
from .llm import OpenAICompatiblePlanner
from .logging_utils import configure_logging, log_event
from .models import (
    JobRecord,
    ScopeContext,
    SourceRecord,
    WebhookResolvePromotion,
    WebhookResolveQuestion,
    WebhookResolveSource,
)
from .notion import NotionClient
from .paths import ScopedPaths
from .repository import NotionRepository
from .sources import SourceFetcher
from .worker import Worker


LOGGER = logging.getLogger(__name__)

_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _bind_host_is_loopback_only(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_BIND_HOSTS


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
        entities_data_source_id=settings.entities_data_source_id,
        questions_data_source_id=settings.questions_data_source_id,
        promotions_data_source_id=settings.promotions_data_source_id,
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

    def _create_job(self, **kwargs):
        return self.worker.repository.create_job(**kwargs)

    def _job_from_source_record(self, source: SourceRecord, *, trigger_type: str) -> tuple[JobRecord, str]:
        event_class = "trigger_regeneration" if source.trigger_regeneration else "source_update"
        suffix = source.checksum or source.last_edited_time or str(source.content_version or 0)
        job_type = "update_wiki" if source.trigger_regeneration else "ingest_source"
        title_prefix = "Regenerate wiki from" if source.trigger_regeneration else "Ingest"
        key = f"{source.source_id}:{source.scope}:{source.owner or '-'}:{event_class}:{suffix}"
        job = self._create_job(
            job_type=job_type,
            title=f"{title_prefix} {source.title}",
            target_source_page_id=source.page_id,
            idempotency_key=key,
            scope_context=source.scope_context,
            policy_page_id=self.worker.repository.active_policy_page_id(source.scope_context),
            trigger_type=trigger_type,
        )
        return job, event_class

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

    def _webhook_state_dir(self) -> Path:
        path = self.settings.wiki_root / "state" / "webhook"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _record_webhook_state(self, name: str, payload: dict[str, Any]) -> None:
        path = self._webhook_state_dir() / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def webhook_status(self) -> dict[str, Any]:
        last_delivery = None
        delivery_path = self._webhook_state_dir() / "last_delivery.json"
        if delivery_path.exists():
            last_delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        verification = None
        verification_path = self._webhook_state_dir() / "last_verification.json"
        if verification_path.exists():
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
        return {
            "ready": bool(self.settings.notion_webhook_signing_secret),
            "public_base_url": self.settings.public_base_url,
            "endpoint": None if not self.settings.public_base_url else f"{self.settings.public_base_url.rstrip('/')}/notion/webhook",
            "has_signing_secret": bool(self.settings.notion_webhook_signing_secret),
            "has_verification_token": bool(self.settings.notion_webhook_verification_token),
            "last_delivery": last_delivery,
            "last_verification": verification,
        }

    def _signed(self, raw_body: bytes, signature: str | None) -> bool:
        # HMAC uses NOTION_WEBHOOK_SIGNING_SECRET only; NOTION_WEBHOOK_VERIFICATION_TOKEN is for handshake payloads.
        secret = self.settings.notion_webhook_signing_secret
        if not secret or not signature:
            return False
        presented = signature.strip()
        if presented.startswith("sha256="):
            presented = presented.split("=", 1)[1]
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(presented, expected)

    def handle_webhook(self, raw_body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid json"}
        log_event(LOGGER, "webhook_received", payload_type=payload.get("type"))
        verification_token = payload.get("verification_token")
        configured_token = self.settings.notion_webhook_verification_token
        if verification_token:
            if not configured_token:
                return HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "NOTION_WEBHOOK_VERIFICATION_TOKEN is not configured (required for subscription handshake)"
                }
            if verification_token != configured_token:
                return HTTPStatus.FORBIDDEN, {"error": "verification token mismatch"}
            verification_payload = {"verified_at": payload.get("timestamp"), "verification_token": verification_token}
            self._record_webhook_state("last_verification", verification_payload)
            return HTTPStatus.OK, {"ok": True}
        if not self.settings.notion_webhook_signing_secret:
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "NOTION_WEBHOOK_SIGNING_SECRET is not configured (required for signed webhook deliveries)"
            }
        signature = headers.get("X-Notion-Signature")
        if not self._signed(raw_body, signature):
            return HTTPStatus.UNAUTHORIZED, {"error": "invalid signature"}
        event_type = payload.get("type") or "unknown"
        entity = payload.get("entity") or {}
        if entity.get("type") != "page" or not entity.get("id"):
            return HTTPStatus.OK, {"accepted": False, "reason": "event entity is not a page"}
        resolve = getattr(self.worker.repository, "resolve_webhook_page", None)
        if not callable(resolve):
            return HTTPStatus.OK, {"accepted": False, "reason": "page is not a supported control-plane row"}
        resolved = resolve(entity["id"])
        if resolved is None:
            return HTTPStatus.OK, {"accepted": False, "reason": "page is not a supported control-plane row"}
        if isinstance(resolved, WebhookResolveSource):
            job, event_class = self._job_from_source_record(resolved.source, trigger_type="webhook")
        elif isinstance(resolved, WebhookResolveQuestion):
            question = resolved.question
            if question.status == "archived":
                return HTTPStatus.OK, {"accepted": False, "reason": "question is archived"}
            key = f"{question.question_id}:{question.scope}:{question.owner or '-'}:answer_question:{question.status}"
            job = self._create_job(
                job_type="answer_question",
                title=f"Answer {question.question[:80]}",
                target_question_page_id=question.page_id,
                idempotency_key=key,
                scope_context=question.scope_context,
                policy_page_id=self.worker.repository.active_policy_page_id(question.scope_context),
                trigger_type="webhook",
            )
            event_class = "question_update"
        elif isinstance(resolved, WebhookResolvePromotion):
            promotion = resolved.promotion
            if promotion.status != "approved":
                return HTTPStatus.OK, {"accepted": False, "reason": "promotion is not approved"}
            key = f"{promotion.promotion_id}:{promotion.scope}:{promotion.owner or '-'}:promote_private:{promotion.status}"
            job = self._create_job(
                job_type="promote_private",
                title=f"Promote {promotion.promotion_id}",
                target_promotion_page_id=promotion.page_id,
                idempotency_key=key,
                scope_context=ScopeContext("shared"),
                policy_page_id=self.worker.repository.active_policy_page_id(ScopeContext("shared")),
                trigger_type="webhook",
            )
            event_class = "promotion_approved"
        else:
            return HTTPStatus.OK, {"accepted": False, "reason": "page is not a supported control-plane row"}
        delivery = {
            "received_at": payload.get("timestamp"),
            "event_type": event_type,
            "event_class": event_class,
            "entity_id": entity["id"],
            "job_id": job.job_id,
        }
        self._record_webhook_state("last_delivery", delivery)
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
        if self.path == "/notion/webhook/status":
            _json_response(self, HTTPStatus.OK, self.server.app.webhook_status())
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
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return
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
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return
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
    if not settings.admin_api_key:
        LOGGER.warning(
            "ADMIN_API_KEY is unset: /admin/* is unauthenticated. Set ADMIN_API_KEY for any non-loopback deployment."
        )
        if not _bind_host_is_loopback_only(host) and not settings.allow_insecure_admin:
            raise SystemExit(
                "Refusing to start: ADMIN_API_KEY is unset while binding to a non-loopback address. "
                "Set ADMIN_API_KEY, use --host 127.0.0.1 (or ::1 / localhost), or set LLMWIKI_INSECURE_ADMIN=1 "
                "(not recommended outside local development)."
            )
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
