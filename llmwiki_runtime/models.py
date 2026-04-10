from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EXECUTABLE_JOB_TYPES = {"ingest_source", "update_wiki"}
ALLOWED_PAGE_TYPES = {"source", "concept", "synthesis", "changelog"}
ALLOWED_OP_TYPES = {"create_file", "patch_sections", "append_block", "no_op"}
JOB_PHASES = {"running", "validating_plan", "applying_changes", "syncing_state"}
JOB_STATUSES = {"queued", "running", "succeeded", "failed"}


@dataclass
class SourceRecord:
    page_id: str
    source_id: str
    source_type: str
    title: str
    canonical_url: str | None
    trust_level: str | None
    status: str | None
    content_version: int | None = None
    checksum: str | None = None
    trigger_regeneration: bool = False
    raw_text_pointer: str | None = None
    markdown_pointer: str | None = None
    source_summary_pointer: str | None = None
    last_edited_time: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRecord:
    page_id: str
    job_id: str
    job_type: str
    status: str
    queue_timestamp: str | None
    target_source_page_id: str | None = None
    target_wiki_page_id: str | None = None
    idempotency_key: str | None = None
    policy_page_id: str | None = None
    attempt_count: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)


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
    summary: str

