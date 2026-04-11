from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .config import Settings
from .frontmatter import dump_document
from .models import ScopeContext, WikiPageMetadata
from .notion import checkbox_property, relation_property, rich_text_property, select_property, title_property, url_property
from .service import ServiceApp, build_worker
from .wiki_ops import ensure_owner_scope, ensure_wiki_root


def _report_dir(root: Path) -> Path:
    path = root / "state" / "live_verification"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_report(root: Path, scenario: str, payload: dict[str, Any]) -> Path:
    path = _report_dir(root) / f"{scenario}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run_until_idle(worker, *, limit: int = 8) -> list[str]:
    processed: list[str] = []
    for _ in range(limit):
        job = worker.run_once()
        if job is None:
            break
        processed.append(job.job_id)
    return processed


def _source_props(source_id: str, title: str, scope_context: ScopeContext, canonical_url: str) -> dict[str, Any]:
    return {
        "Source Title": title_property(title),
        "Source ID": rich_text_property(source_id),
        "Scope": select_property(scope_context.scope),
        "Owner": rich_text_property(scope_context.owner or ""),
        "Source Type": select_property("web_page"),
        "Canonical URL": url_property(canonical_url),
        "Trust Level": select_property("primary"),
        "Source Status": select_property("queued"),
        "Trigger Regeneration": checkbox_property(False),
        "Content Version": {"number": 1},
    }


def _question_props(question_id: str, text: str, scope_context: ScopeContext) -> dict[str, Any]:
    return {
        "Question": title_property(text),
        "Question ID": rich_text_property(question_id),
        "Question Status": select_property("queued"),
        "Scope": select_property(scope_context.scope),
        "Owner": rich_text_property(scope_context.owner or ""),
    }


def _promotion_props(promotion_id: str, scope_context: ScopeContext, source_private_page_id: str) -> dict[str, Any]:
    return {
        "Promotion ID": title_property(promotion_id),
        "Scope": select_property(scope_context.scope),
        "Owner": rich_text_property(scope_context.owner or ""),
        "Status": select_property("approved"),
        "Decision": rich_text_property("Approved by live verification"),
        "Submitted By": rich_text_property(scope_context.owner or "live-verify"),
        "Reviewed By": rich_text_property("live-verify"),
        "Source Private Page": relation_property([source_private_page_id]),
    }


def _create_private_wiki_row(worker, *, owner: str, slug: str, title: str) -> tuple[str, str]:
    ensure_owner_scope(worker.wiki_root, owner)
    relative_path = f"wiki/users/{owner}/concepts/{slug}.md"
    absolute_path = worker.wiki_root / relative_path
    document = dump_document(
        {
            "title": title,
            "page_type": "concept",
            "slug": slug,
            "status": "draft",
            "updated_at": "2026-04-10T00:00:00Z",
            "source_ids": [],
            "source_scope": [],
            "entity_keys": [],
            "concept_keys": [slug],
            "confidence": "medium",
            "review_required": False,
            "scope": "private",
            "owner": owner,
            "review_state": "n_a",
            "promotion_origin": None,
        },
        "# Live Verification Private Page\n\n## One-line summary\nPrivate summary.\n\n## Key points\n- Internal-only point.\n\n## Details\nPrivate details.\n\n## Evidence\n- Operator note.\n\n## Open questions\n\n## Related pages\n\n## Change log\n- created\n\n## Sources\n",
    )
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(document, encoding="utf-8")
    page = worker.repository.client.create_page(
        worker.repository.wiki_data_source_id,
        {
            "Wiki Title": title_property(title),
            "Wiki Slug": rich_text_property(slug),
            "Wiki Type": select_property("concept"),
            "Wiki Status": select_property("draft"),
            "Scope": select_property("private"),
            "Owner": rich_text_property(owner),
            "Canonical Markdown Path": rich_text_property(relative_path),
            "Summary": rich_text_property("Private summary."),
            "Confidence Level": select_property("medium"),
            "Needs Human Review": checkbox_property(False),
            "Review State": select_property("n_a"),
        },
    )
    return page["id"], relative_path


def _run_source_scenario(worker, prefix: str) -> dict[str, Any]:
    source_page = worker.repository.client.create_page(
        worker.repository.sources_data_source_id,
        _source_props(f"{prefix}_source", "Live Verify Source", ScopeContext("shared"), "https://www.example.com/"),
    )
    job = worker.enqueue_ingest_job(source_page["id"])
    processed_jobs = [job.job_id] + _run_until_idle(worker)
    source = worker.repository.get_source(source_page["id"])
    source_summary_path = source.source_summary_pointer
    return {
        "scenario": "source",
        "source_page_id": source.page_id,
        "jobs": processed_jobs,
        "source_status": source.status,
        "source_summary_pointer": source_summary_path,
        "passed": source.status == "processed" and bool(source_summary_path),
    }


def _run_question_scenario(worker, prefix: str) -> dict[str, Any]:
    if not worker.repository.questions_data_source_id:
        return {"scenario": "question", "passed": False, "reason": "Questions data source is not configured"}
    question_page = worker.repository.client.create_page(
        worker.repository.questions_data_source_id,
        _question_props(f"{prefix}_question", "What does the live verification source say?", ScopeContext("shared")),
    )
    job = worker.enqueue_question_job(question_page["id"])
    processed_jobs = [job.job_id] + _run_until_idle(worker)
    question = worker.repository.get_question(question_page["id"])
    passed = bool(question.answer_page_slug and question.resolution_type in {"faq", "open_question"})
    return {
        "scenario": "question",
        "question_page_id": question.page_id,
        "jobs": processed_jobs,
        "question_status": question.status,
        "answer_page_slug": question.answer_page_slug,
        "resolution_type": question.resolution_type,
        "passed": passed,
    }


def _run_promotion_scenario(worker, prefix: str) -> dict[str, Any]:
    if not worker.repository.promotions_data_source_id:
        return {"scenario": "promotion", "passed": False, "reason": "Promotions data source is not configured"}
    private_page_id, private_path = _create_private_wiki_row(worker, owner="liveverify", slug=f"{prefix}-private", title="Live Verify Private Concept")
    promotion_page = worker.repository.client.create_page(
        worker.repository.promotions_data_source_id,
        _promotion_props(f"{prefix}_promotion", ScopeContext("private", "liveverify"), private_page_id),
    )
    job = worker.enqueue_promotion_job(promotion_page["id"])
    processed_jobs = [job.job_id] + _run_until_idle(worker)
    promotion = worker.repository.get_promotion(promotion_page["id"])
    log_path = worker.wiki_root / "state" / "promotion_logs" / f"{promotion.promotion_id}.json"
    approved_path = worker.wiki_root / "reviews" / "approved" / f"{promotion.promotion_id}.json"
    return {
        "scenario": "promotion",
        "promotion_page_id": promotion.page_id,
        "private_source_path": private_path,
        "jobs": processed_jobs,
        "promotion_status": promotion.status,
        "promotion_log": str(log_path),
        "approved_record": str(approved_path),
        "passed": promotion.status == "applied" and log_path.exists() and approved_path.exists(),
    }


def _run_webhook_scenario(settings: Settings, worker, prefix: str) -> dict[str, Any]:
    ensure_wiki_root(worker.wiki_root)
    app = ServiceApp(settings=settings, worker=worker)
    source_page = worker.repository.client.create_page(
        worker.repository.sources_data_source_id,
        _source_props(f"{prefix}_webhook_source", "Live Verify Webhook Source", ScopeContext("shared"), "https://www.example.com/"),
    )
    payload = {
        "type": "page.properties_updated",
        "timestamp": "2026-04-10T00:00:00Z",
        "entity": {"id": source_page["id"], "type": "page"},
    }
    raw_body = json.dumps(payload, sort_keys=True).encode("utf-8")
    signature = None
    if settings.notion_webhook_signing_secret:
        import hmac
        import hashlib

        digest = hmac.new(settings.notion_webhook_signing_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"
    status, response = app.handle_webhook(raw_body, {"X-Notion-Signature": signature or ""})
    processed_jobs = _run_until_idle(worker)
    return {
        "scenario": "webhook",
        "source_page_id": source_page["id"],
        "response_status": status,
        "response": response,
        "jobs": processed_jobs,
        "passed": status == 200 and response.get("accepted") is True,
    }


def run_live_verification(settings: Settings, *, scenario: str, cleanup_mode: str = "keep") -> dict[str, Any]:
    worker = build_worker(settings)
    prefix = "live_verify"
    scenarios = []
    if scenario in {"source", "full"}:
        scenarios.append(_run_source_scenario(worker, prefix))
    if scenario in {"question", "full"}:
        scenarios.append(_run_question_scenario(worker, prefix))
    if scenario in {"promotion", "full"}:
        scenarios.append(_run_promotion_scenario(worker, prefix))
    if scenario in {"webhook", "full"}:
        scenarios.append(_run_webhook_scenario(settings, worker, prefix))
    report = {
        "scenario": scenario,
        "cleanup_mode": cleanup_mode,
        "results": scenarios,
        "passed": all(item.get("passed") for item in scenarios),
    }
    report_path = _write_report(settings.wiki_root, scenario, report)
    report["report_path"] = str(report_path)
    return report
