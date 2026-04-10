from __future__ import annotations

from typing import Any

import hashlib
from http import HTTPStatus
import hmac
import json
from pathlib import Path
import tempfile
import threading
from urllib import error, request
import unittest

from llmwiki_runtime.config import Settings
from llmwiki_runtime.models import JobRecord, ScopeContext, SourceRecord, WebhookResolveSource
from llmwiki_runtime.service import LLMWikiHTTPServer, ServiceApp


class HTTPStubRepository:
    def __init__(self) -> None:
        self.created_jobs: list[tuple[str, str]] = []
        self.inspect_jobs_result = [
            JobRecord(
                page_id="job-page-id",
                job_id="job_1",
                job_type="ingest_source",
                status="queued",
                queue_timestamp=None,
                scope="shared",
                owner=None,
                target_source_page_id="source-page-id",
                idempotency_key="key-1",
            )
        ]
        self.source = SourceRecord(
            page_id="source-page-id",
            source_id="src_1",
            source_type="web_page",
            title="Source",
            canonical_url="https://example.com",
            trust_level="primary",
            status="queued",
            scope="shared",
            properties={"Source Title": {}},
        )
        self.raise_unreadable = False

    def query_jobs(self, *, status: str | None = None, page_size: int = 20):
        return list(self.inspect_jobs_result)

    def requeue_job(self, job_page_id: str):
        return JobRecord(
            page_id=job_page_id,
            job_id="job_2",
            job_type="ingest_source",
            status="queued",
            queue_timestamp=None,
            scope="shared",
            owner=None,
        )

    def get_source(self, source_page_id: str):
        if self.raise_unreadable:
            raise error.HTTPError("https://example.com", 404, "missing", {}, None)
        return self.source

    def active_policy_page_id(self, scope_context: ScopeContext | None = None) -> str:
        return "policy-page-id"

    def create_job(
        self,
        *,
        job_type: str,
        title: str,
        target_source_page_id: str,
        idempotency_key: str,
        scope_context: ScopeContext,
        policy_page_id: str | None = None,
        **kwargs: Any,
    ):
        self.created_jobs.append((job_type, idempotency_key))
        return type("Job", (), {"job_id": "job-created", "scope": scope_context.scope, "owner": scope_context.owner})()

    def resolve_webhook_page(self, page_id: str) -> WebhookResolveSource | None:
        try:
            source = self.get_source(page_id)
        except error.HTTPError:
            return None
        if source.properties.get("Source Title") is None:
            return None
        return WebhookResolveSource(source=source)


class HTTPStubWorker:
    def __init__(self) -> None:
        self.repository = HTTPStubRepository()

    def enqueue_ingest_job(self, source_page_id: str):
        return JobRecord(
            page_id="job-page-id",
            job_id="job_1",
            job_type="ingest_source",
            status="queued",
            queue_timestamp=None,
            scope="shared",
            owner=None,
            target_source_page_id=source_page_id,
        )


class HTTPServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir_ctx = tempfile.TemporaryDirectory()
        self.settings = Settings(
            notion_token="token",
            notion_version="2026-03-11",
            notion_api_base="https://api.notion.com/v1",
            control_db_id=None,
            sources_data_source_id="sources",
            wiki_data_source_id="wiki",
            jobs_data_source_id="jobs",
            policies_data_source_id="policies",
            wiki_root=Path(self.tmpdir_ctx.name),
            worker_name="worker",
            poll_interval_seconds=5,
            admin_api_key="admin-key",
            llm_api_key=None,
            llm_api_base="https://example.com/v1",
            llm_model=None,
            notion_webhook_signing_secret="signing-secret",
            notion_webhook_verification_token="verify-token",
            log_level="INFO",
        )
        self.worker = HTTPStubWorker()
        self.app = ServiceApp(settings=self.settings, worker=self.worker)
        self.server = LLMWikiHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.tmpdir_ctx.cleanup()

    def _request(self, method: str, path: str, *, payload: dict | None = None, headers: dict[str, str] | None = None):
        data = None
        request_headers = {} if headers is None else dict(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = request.Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _request_error(self, method: str, path: str, *, payload: dict | None = None, headers: dict[str, str] | None = None):
        data = None
        request_headers = {} if headers is None else dict(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = request.Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with self.assertRaises(error.HTTPError) as ctx:
            request.urlopen(req)
        return ctx.exception.code, json.loads(ctx.exception.read().decode("utf-8"))

    def test_healthz(self) -> None:
        status, payload = self._request("GET", "/healthz")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["status"], "ok")

    def test_admin_jobs_requires_key(self) -> None:
        status, payload = self._request_error("GET", "/admin/jobs")
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(payload["error"], "invalid admin key")

    def test_admin_jobs_returns_result(self) -> None:
        status, payload = self._request("GET", "/admin/jobs?status=queued", headers={"X-Admin-Key": "admin-key"})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["jobs"][0]["job_id"], "job_1")

    def test_admin_enqueue_requires_source_page_id(self) -> None:
        status, payload = self._request_error("POST", "/admin/enqueue/source", payload={}, headers={"X-Admin-Key": "admin-key"})
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(payload["error"], "source_page_id is required")

    def test_admin_enqueue_rejects_bad_key(self) -> None:
        status, payload = self._request_error(
            "POST",
            "/admin/enqueue/source",
            payload={"source_page_id": "source-page-id"},
            headers={"X-Admin-Key": "bad"},
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(payload["error"], "invalid admin key")

    def test_admin_enqueue_accepts_valid_request(self) -> None:
        status, payload = self._request(
            "POST",
            "/admin/enqueue/source",
            payload={"source_page_id": "source-page-id"},
            headers={"X-Admin-Key": "admin-key"},
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(payload["job_id"], "job_1")

    def test_admin_requeue_requires_job_page_id(self) -> None:
        status, payload = self._request_error("POST", "/admin/requeue/job", payload={}, headers={"X-Admin-Key": "admin-key"})
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(payload["error"], "job_page_id is required")

    def test_admin_requeue_accepts_valid_request(self) -> None:
        status, payload = self._request(
            "POST",
            "/admin/requeue/job",
            payload={"job_page_id": "job-page-id"},
            headers={"X-Admin-Key": "admin-key"},
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(payload["job_id"], "job_2")

    def test_admin_enqueue_invalid_json_returns_400(self) -> None:
        req = request.Request(
            f"{self.base_url}/admin/enqueue/source",
            data=b"{",
            headers={"Content-Type": "application/json", "X-Admin-Key": "admin-key"},
            method="POST",
        )
        with self.assertRaises(error.HTTPError) as ctx:
            request.urlopen(req)
        self.assertEqual(ctx.exception.code, HTTPStatus.BAD_REQUEST)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "invalid json")

    def test_admin_requeue_invalid_json_returns_400(self) -> None:
        req = request.Request(
            f"{self.base_url}/admin/requeue/job",
            data=b"not-json",
            headers={"Content-Type": "application/json", "X-Admin-Key": "admin-key"},
            method="POST",
        )
        with self.assertRaises(error.HTTPError) as ctx:
            request.urlopen(req)
        self.assertEqual(ctx.exception.code, HTTPStatus.BAD_REQUEST)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "invalid json")

    def test_webhook_invalid_signature(self) -> None:
        status, payload = self._request_error(
            "POST",
            "/notion/webhook",
            payload={"type": "page.properties_updated", "entity": {"id": "source-page-id", "type": "page"}},
            headers={"X-Notion-Signature": "sha256=bad"},
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(payload["error"], "invalid signature")

    def test_webhook_invalid_json_returns_400(self) -> None:
        req = request.Request(
            f"{self.base_url}/notion/webhook",
            data=b"not-json{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(error.HTTPError) as ctx:
            request.urlopen(req)
        self.assertEqual(ctx.exception.code, HTTPStatus.BAD_REQUEST)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "invalid json")

    def test_webhook_verification_handshake(self) -> None:
        status, payload = self._request("POST", "/notion/webhook", payload={"verification_token": "verify-token"})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["ok"])

    def test_webhook_non_page_entity(self) -> None:
        body = {"type": "page.properties_updated", "entity": {"id": "block-id", "type": "block"}}
        signature = hmac.new(b"signing-secret", json.dumps(body).encode("utf-8"), hashlib.sha256).hexdigest()
        status, payload = self._request(
            "POST",
            "/notion/webhook",
            payload=body,
            headers={"X-Notion-Signature": f"sha256={signature}"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(payload["accepted"])

    def test_webhook_unreadable_page(self) -> None:
        self.worker.repository.raise_unreadable = True
        body = {"type": "page.properties_updated", "entity": {"id": "source-page-id", "type": "page"}}
        signature = hmac.new(b"signing-secret", json.dumps(body).encode("utf-8"), hashlib.sha256).hexdigest()
        status, payload = self._request(
            "POST",
            "/notion/webhook",
            payload=body,
            headers={"X-Notion-Signature": f"sha256={signature}"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(payload["accepted"])

    def test_webhook_trigger_regeneration_creates_update_job(self) -> None:
        self.worker.repository.source.trigger_regeneration = True
        body = {"type": "page.properties_updated", "entity": {"id": "source-page-id", "type": "page"}}
        signature = hmac.new(b"signing-secret", json.dumps(body).encode("utf-8"), hashlib.sha256).hexdigest()
        status, payload = self._request(
            "POST",
            "/notion/webhook",
            payload=body,
            headers={"X-Notion-Signature": f"sha256={signature}"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["accepted"])
        self.assertEqual(self.worker.repository.created_jobs[-1][0], "update_wiki")


if __name__ == "__main__":
    unittest.main()
