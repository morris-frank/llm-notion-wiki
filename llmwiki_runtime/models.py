from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EXECUTABLE_JOB_TYPES = {"ingest_source", "update_wiki", "answer_question", "promote_private"}
ALLOWED_PAGE_TYPES = {"source", "concept", "entity", "faq", "question", "synthesis", "index", "changelog"}
ALLOWED_OP_TYPES = {"create_file", "patch_sections", "append_block", "no_op"}
JOB_PHASES = {"running", "validating_plan", "applying_changes", "syncing_state"}
JOB_STATUSES = {"queued", "running", "succeeded", "failed"}
SCOPES = {"shared", "private"}
QUESTION_RESOLUTION_TYPES = {"open_question", "faq"}


@dataclass(frozen=True)
class ScopeContext:
    scope: str
    owner: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"Unsupported scope: {self.scope}")
        if self.scope == "shared" and self.owner:
            raise ValueError("Shared scope cannot have an owner")
        if self.scope == "private" and not self.owner:
            raise ValueError("Private scope requires an owner")

    @property
    def owner_or_null(self) -> str | None:
        return self.owner


@dataclass
class SourceRecord:
    page_id: str
    source_id: str
    source_type: str
    title: str
    canonical_url: str | None
    trust_level: str | None
    status: str | None
    scope: str = "shared"
    owner: str | None = None
    target_page_id: str | None = None
    content_version: int | None = None
    checksum: str | None = None
    trigger_regeneration: bool = False
    raw_text_pointer: str | None = None
    markdown_pointer: str | None = None
    source_summary_pointer: str | None = None
    last_edited_time: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_context(self) -> ScopeContext:
        return ScopeContext(self.scope, self.owner)


@dataclass
class JobRecord:
    page_id: str
    job_id: str
    job_type: str
    status: str
    queue_timestamp: str | None
    scope: str = "shared"
    owner: str | None = None
    target_source_page_id: str | None = None
    target_wiki_page_id: str | None = None
    target_question_page_id: str | None = None
    target_promotion_page_id: str | None = None
    idempotency_key: str | None = None
    policy_page_id: str | None = None
    attempt_count: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_context(self) -> ScopeContext:
        return ScopeContext(self.scope, self.owner)


@dataclass
class SourceArtifacts:
    metadata: dict[str, Any]
    raw_text: str
    markdown: str
    checksum: str
    storage_dir: Path


@dataclass
class SectionPatch:
    section: str
    action: str
    content: str
    match_key: str | None = None


@dataclass
class Operation:
    op: str
    path: str
    page_type: str
    reason: str
    content: str | None = None
    previous_content_sha256: str | None = None
    content_sha256: str | None = None
    section_patches: list[SectionPatch] = field(default_factory=list)


@dataclass
class RunPlan:
    schema_version: str
    job_id: str
    source_id: str
    run_mode: str
    summary: dict[str, Any]
    touched_paths: list[str]
    operations: list[Operation]
    manifest_update: dict[str, Any]
    warnings: list[str]


@dataclass
class WikiPageMetadata:
    path: str
    title: str
    slug: str
    page_type: str
    status: str
    confidence: str
    review_required: bool
    source_ids: list[str]
    source_scope: list[str]
    scope: str
    owner: str | None
    review_state: str
    promotion_origin: str | None
    summary: str
    entity_keys: list[str] = field(default_factory=list)
    entity_type: str | None = None

    @property
    def scope_context(self) -> ScopeContext:
        return ScopeContext(self.scope, self.owner)


@dataclass
class PolicyRecord:
    page_id: str
    name: str
    version: str | None
    target_scope: str
    owner: str | None
    priority: int
    active: bool
    allowed_page_types: list[str]
    question_mode: str
    entity_extraction: str
    promotion_required_for_shared: bool
    minimum_review_state_for_shared: str | None
    requires_human_review: bool
    auto_publish_allowed: bool
    max_source_count: int | None
    prompt_bundle_pointer: str | None
    citation_policy_pointer: str | None
    page_template_pointer: str | None
    content_markdown: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuestionRecord:
    page_id: str
    question_id: str
    question: str
    status: str
    scope: str = "shared"
    owner: str | None = None
    latest_job_page_id: str | None = None
    target_wiki_page_id: str | None = None
    answer_page_slug: str | None = None
    resolution_type: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_context(self) -> ScopeContext:
        return ScopeContext(self.scope, self.owner)


@dataclass
class EntityRecord:
    page_id: str
    canonical_entity_id: str
    name: str
    entity_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionRecord:
    page_id: str
    promotion_id: str
    scope: str
    owner: str | None
    status: str
    decision: str | None
    submitted_by: str | None
    reviewed_by: str | None
    source_private_page_id: str | None
    target_shared_page_ids: list[str]
    latest_job_page_id: str | None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_context(self) -> ScopeContext:
        return ScopeContext(self.scope, self.owner)


@dataclass(frozen=True)
class WebhookResolveSource:
    source: SourceRecord


@dataclass(frozen=True)
class WebhookResolveQuestion:
    question: QuestionRecord


@dataclass(frozen=True)
class WebhookResolvePromotion:
    promotion: PromotionRecord


WebhookResolveResult = WebhookResolveSource | WebhookResolveQuestion | WebhookResolvePromotion
