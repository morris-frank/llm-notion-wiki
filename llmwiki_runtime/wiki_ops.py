from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .contracts import (
    FILE_OPERATION_CONTRACT,
    OPERATION_SCHEMA_VERSION,
    SCHEMA_PRIVATE,
    SCHEMA_PROMOTION,
    SCHEMA_SHARED,
    SCHEMA_TAXONOMY,
    maintainer_contract,
)
from .frontmatter import dump_document, parse_document
from .models import ALLOWED_OP_TYPES, ALLOWED_PAGE_TYPES, Operation, RunPlan, ScopeContext, SectionPatch, WikiPageMetadata
from .paths import CHANGELOG_SEED_FILENAME, ScopedPaths, page_type_matches_path, scope_root_directories


GENERIC_PAGE_SECTIONS = [
    "## One-line summary",
    "## Key points",
    "## Details",
    "## Evidence",
    "## Open questions",
    "## Related pages",
    "## Change log",
    "## Sources",
]
SOURCE_PAGE_SECTIONS = [
    "## One-line summary",
    "## Source summary",
    "## Main claims",
    "## Important entities",
    "## Important concepts",
    "## Reliability notes",
    "## Related pages",
    "## Change log",
    "## Sources",
]
MAX_TOUCHED_FILES = 5
MAX_SECTION_PATCHES = 20
MAX_FILE_BYTES = 200_000


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_page_body(title: str, related_link: str) -> str:
    return f"""# {title}

## One-line summary
Bootstrapped page for {title.lower()}.

## Key points
- Created by the runtime.

## Details

## Evidence

## Open questions

## Related pages
- [[{related_link}]]

## Change log
- Bootstrapped by runtime.

## Sources
"""


def _seed_page_metadata(title: str, page_type: str, slug: str, scope_context: ScopeContext) -> OrderedDict[str, object]:
    return OrderedDict(
        [
            ("title", title),
            ("page_type", page_type),
            ("slug", slug),
            ("status", "draft"),
            ("updated_at", utcnow_iso()),
            ("source_ids", []),
            ("source_scope", []),
            ("entity_keys", []),
            ("concept_keys", []),
            ("confidence", "medium"),
            ("review_required", False),
            ("scope", scope_context.scope),
            ("owner", scope_context.owner_or_null),
            ("review_state", "unreviewed" if scope_context.scope == "shared" else "n/a"),
            ("promotion_origin", None),
        ]
    )


def _seed_pages_for_scope(scope_context: ScopeContext, root: Path) -> dict[Path, tuple[OrderedDict[str, object], str]]:
    scoped_paths = ScopedPaths(root, scope_context)
    return {
        scoped_paths.index_page_path(): (
            _seed_page_metadata("Wiki Index", "index", "index", scope_context),
            _seed_page_body("Wiki Index", "current-state"),
        ),
        scoped_paths.synthesis_page_path(): (
            _seed_page_metadata("Current State", "synthesis", "current-state", scope_context),
            _seed_page_body("Current State", "index"),
        ),
        scoped_paths.changelog_page_path(): (
            _seed_page_metadata("Ingest Log", "changelog", "ingest-log", scope_context),
            _seed_page_body("Ingest Log", "index"),
        ),
    }


def ensure_scope_root(root: Path, scope_context: ScopeContext) -> None:
    for path in scope_root_directories(root, owner=scope_context.owner):
        path.mkdir(parents=True, exist_ok=True)
    schema_files = {
        root / "schema" / "shared.md": SCHEMA_SHARED,
        root / "schema" / "private.md": SCHEMA_PRIVATE,
        root / "schema" / "promotion.md": SCHEMA_PROMOTION,
        root / "schema" / "taxonomy.md": SCHEMA_TAXONOMY,
        root / "config" / "file-operation-contract.md": FILE_OPERATION_CONTRACT,
        root / "AGENTS.shared.md": maintainer_contract(ScopeContext("shared")),
        root / "AGENTS.private.template.md": maintainer_contract(ScopeContext("private", "owner")),
    }
    for path, content in schema_files.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    for path, (metadata, body) in _seed_pages_for_scope(scope_context, root).items():
        if not path.exists():
            path.write_text(dump_document(metadata, body), encoding="utf-8")


def ensure_wiki_root(root: Path) -> None:
    ensure_scope_root(root, ScopeContext("shared"))


def ensure_owner_scope(root: Path, owner: str) -> None:
    ensure_scope_root(root, ScopeContext("private", owner))


def _validate_relative_wiki_path(path: str, page_type: str, scope_context: ScopeContext) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"Unsafe path: {path}")
    if not path.endswith(".md"):
        raise ValueError(f"Wiki paths must end in .md: {path}")
    if page_type not in ALLOWED_PAGE_TYPES:
        raise ValueError(f"Disallowed page type: {page_type}")
    if not page_type_matches_path(path, page_type, scope_context):
        raise ValueError(f"Path {path} does not match page type {page_type} and scope {scope_context.scope}")


def _page_type_for_path(path: str, scope_context: ScopeContext) -> str:
    for page_type in ALLOWED_PAGE_TYPES:
        if page_type_matches_path(path, page_type, scope_context):
            if page_type == "index" and path.endswith(CHANGELOG_SEED_FILENAME):
                return "changelog"
            return page_type
    raise ValueError(f"Manifest path is outside the allowed scope: {path}")


def _validate_manifest_payload(manifest: dict[str, Any], *, scoped_paths: ScopedPaths, source_id: str) -> dict[str, Any]:
    if manifest.get("source_id") != source_id:
        raise ValueError(f"Manifest source_id mismatch for {source_id}")
    if manifest.get("scope") != scoped_paths.scope_context.scope:
        raise ValueError(f"Manifest scope mismatch for {source_id}")
    if manifest.get("owner") != scoped_paths.scope_context.owner_or_null:
        raise ValueError(f"Manifest owner mismatch for {source_id}")
    source_page = manifest.get("source_page")
    expected_source_page = scoped_paths.relative(scoped_paths.source_page_path(source_id))
    if source_page != expected_source_page:
        raise ValueError(f"Manifest source_page mismatch for {source_id}")
    _validate_relative_wiki_path(source_page, "source", scoped_paths.scope_context)
    affected_pages = manifest.get("affected_pages")
    if not isinstance(affected_pages, list):
        raise ValueError(f"Manifest affected_pages must be a list for {source_id}")
    validated_pages: list[str] = []
    for path in affected_pages:
        if not isinstance(path, str):
            raise ValueError(f"Manifest affected_pages contains non-string entry for {source_id}")
        page_type = _page_type_for_path(path, scoped_paths.scope_context)
        _validate_relative_wiki_path(path, page_type, scoped_paths.scope_context)
        validated_pages.append(path)
    manifest = dict(manifest)
    manifest["affected_pages"] = sorted(set(validated_pages))
    return manifest


def parse_run_plan(raw_json: str) -> RunPlan:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    expected_keys = {
        "schema_version",
        "job_id",
        "source_id",
        "run_mode",
        "summary",
        "touched_paths",
        "operations",
        "manifest_update",
        "warnings",
    }
    extra = set(payload) - expected_keys
    missing = expected_keys - set(payload)
    if extra or missing:
        raise ValueError(f"Unexpected plan keys extra={sorted(extra)} missing={sorted(missing)}")
    operations: list[Operation] = []
    for index, item in enumerate(payload["operations"]):
        op_type = item.get("op")
        if op_type not in ALLOWED_OP_TYPES:
            raise ValueError(f"Unsupported operation {op_type} at index {index}")
        section_patches = [
            SectionPatch(
                section=patch["section"],
                action=patch["action"],
                content=patch["content"],
                match_key=patch.get("match_key"),
            )
            for patch in item.get("section_patches", [])
        ]
        operations.append(
            Operation(
                op=op_type,
                path=item["path"],
                page_type=item["page_type"],
                reason=item["reason"],
                content=item.get("content"),
                previous_content_sha256=item.get("previous_content_sha256"),
                content_sha256=item.get("content_sha256"),
                section_patches=section_patches,
            )
        )
    return RunPlan(
        schema_version=payload["schema_version"],
        job_id=payload["job_id"],
        source_id=payload["source_id"],
        run_mode=payload["run_mode"],
        summary=payload["summary"],
        touched_paths=list(payload["touched_paths"]),
        operations=operations,
        manifest_update=payload["manifest_update"],
        warnings=list(payload["warnings"]),
    )


def validate_run_plan(plan: RunPlan, *, root: Path, scope_context: ScopeContext) -> None:
    if plan.schema_version != OPERATION_SCHEMA_VERSION:
        raise ValueError(f"Unexpected schema version: {plan.schema_version}")
    if plan.run_mode not in {"apply", "dry_run"}:
        raise ValueError(f"Unsupported run mode: {plan.run_mode}")
    if plan.summary.get("decision") == "no_op" and any(op.op != "no_op" for op in plan.operations):
        raise ValueError("no_op decision cannot include write operations")
    touched = {op.path for op in plan.operations if op.op != "no_op"}
    if touched != set(plan.touched_paths):
        raise ValueError("touched_paths must match non-no_op operations exactly")
    affected_pages = plan.manifest_update.get("affected_pages", [])
    if not isinstance(affected_pages, list):
        raise ValueError("manifest_update.affected_pages must be a list")
    if not set(plan.touched_paths).issubset(set(affected_pages)):
        raise ValueError("manifest_update.affected_pages must include all touched paths")
    source_page = plan.manifest_update.get("source_page")
    if not isinstance(source_page, str):
        raise ValueError("manifest_update.source_page must be a string")
    _validate_relative_wiki_path(source_page, "source", scope_context)
    for path in affected_pages:
        if not isinstance(path, str):
            raise ValueError("manifest_update.affected_pages entries must be strings")
        page_type = _page_type_for_path(path, scope_context)
        _validate_relative_wiki_path(path, page_type, scope_context)
    if len(touched) > MAX_TOUCHED_FILES:
        raise ValueError(f"Plan touches too many files: {len(touched)}")
    total_patches = sum(len(op.section_patches) for op in plan.operations)
    if total_patches > MAX_SECTION_PATCHES:
        raise ValueError(f"Plan has too many section patches: {total_patches}")
    for op in plan.operations:
        _validate_relative_wiki_path(op.path, op.page_type, scope_context)
        if op.op == "create_file" and (root / op.path).exists():
            raise ValueError(f"create_file target already exists: {op.path}")
        if op.op == "patch_sections" and not (root / op.path).exists():
            raise ValueError(f"patch_sections target missing: {op.path}")
        if op.op == "append_block" and op.page_type != "changelog":
            raise ValueError("append_block is limited to changelog pages")
        if op.op == "replace_file":
            raise ValueError("replace_file is disabled in v1")


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(re.finditer(r"(?m)^## .+$", body))
    if not matches:
        return body, []
    prefix = body[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = body[start:end]
        heading, _, content = chunk.partition("\n")
        sections.append((heading.strip(), content))
    return prefix, sections


def _rebuild_sections(prefix: str, sections: list[tuple[str, str]]) -> str:
    output = prefix.rstrip("\n") + "\n\n"
    for heading, content in sections:
        output += heading + "\n" + content.lstrip("\n")
        if not output.endswith("\n"):
            output += "\n"
        if not output.endswith("\n\n"):
            output += "\n"
    return output.rstrip() + "\n"


def _upsert_bullet(existing: str, match_key: str, content: str) -> str:
    lines = existing.splitlines()
    for index, line in enumerate(lines):
        if match_key in line:
            lines[index] = content
            return "\n".join(lines).rstrip() + "\n"
    if lines and lines[-1].strip():
        lines.append(content)
    else:
        lines[-1:] = [content]
    return "\n".join(lines).rstrip() + "\n"


def _required_sections(page_type: str) -> list[str]:
    return SOURCE_PAGE_SECTIONS if page_type == "source" else GENERIC_PAGE_SECTIONS


def _merge_metadata(
    document_text: str,
    *,
    source_id: str,
    source_scope: str,
    scope_context: ScopeContext,
    now: str,
) -> tuple[OrderedDict[str, object], str]:
    parsed = parse_document(document_text)
    metadata = parsed.metadata
    metadata["updated_at"] = now
    metadata["scope"] = scope_context.scope
    metadata["owner"] = scope_context.owner_or_null
    if "review_state" not in metadata:
        metadata["review_state"] = "unreviewed" if scope_context.scope == "shared" else "n/a"
    if "promotion_origin" not in metadata:
        metadata["promotion_origin"] = None
    source_ids = list(metadata.get("source_ids", []))
    if source_id not in source_ids:
        source_ids.append(source_id)
    metadata["source_ids"] = source_ids
    source_scopes = list(metadata.get("source_scope", []))
    if source_scope not in source_scopes:
        source_scopes.append(source_scope)
    metadata["source_scope"] = source_scopes
    return metadata, parsed.body


def validate_resulting_document(
    path: str,
    content: str,
    *,
    page_type: str,
    source_id: str,
    source_scope: str,
    scope_context: ScopeContext,
) -> None:
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"Resulting file exceeds size limit: {path}")
    parsed = parse_document(content)
    metadata = parsed.metadata
    required_keys = [
        "title",
        "page_type",
        "slug",
        "status",
        "updated_at",
        "source_ids",
        "source_scope",
        "entity_keys",
        "concept_keys",
        "confidence",
        "review_required",
        "scope",
        "owner",
        "review_state",
        "promotion_origin",
    ]
    for key in required_keys:
        if key not in metadata:
            raise ValueError(f"Missing required frontmatter key {key} in {path}")
    if metadata["page_type"] != page_type:
        raise ValueError(f"Frontmatter page_type mismatch in {path}")
    if metadata["scope"] != scope_context.scope:
        raise ValueError(f"Frontmatter scope mismatch in {path}")
    if metadata["owner"] != scope_context.owner_or_null:
        raise ValueError(f"Frontmatter owner mismatch in {path}")
    if page_type != "changelog" and source_id not in metadata.get("source_ids", []):
        raise ValueError(f"Current source_id missing from source_ids in {path}")
    source_scopes = [str(item) for item in metadata.get("source_scope", [])]
    if source_scope not in source_scopes:
        raise ValueError(f"Current source scope missing from source_scope in {path}")
    if scope_context.scope == "shared" and any(item != "shared" for item in source_scopes):
        raise ValueError(f"Shared pages may not cite private sources in {path}")
    if scope_context.scope == "shared" and ("raw/users/" in content or "wiki/users/" in content):
        raise ValueError(f"Shared pages may not reference private paths in {path}")
    for section in _required_sections(page_type):
        if section not in parsed.body:
            raise ValueError(f"Missing required section {section} in {path}")
    cited_sources = set(re.findall(r"\[S:([^\]]+)\]", parsed.body))
    sources_section_match = re.search(r"(?ms)^## Sources\n(?P<body>.*)$", parsed.body)
    sources_body = sources_section_match.group("body") if sources_section_match else ""
    for citation in cited_sources:
        if f"[S:{citation}]" not in sources_body:
            raise ValueError(f"Citation [S:{citation}] missing from ## Sources in {path}")


def apply_run_plan(
    plan: RunPlan,
    *,
    root: Path,
    scope_context: ScopeContext,
    source_scope: str,
) -> dict[str, str]:
    now = utcnow_iso()
    state: dict[str, str] = {}
    for path in plan.touched_paths:
        absolute = root / path
        if absolute.exists():
            state[path] = absolute.read_text(encoding="utf-8")
    for op in plan.operations:
        if op.op == "no_op":
            continue
        if op.op == "create_file":
            if not op.content:
                raise ValueError(f"create_file requires content for {op.path}")
            metadata, body = _merge_metadata(
                op.content,
                source_id=plan.source_id,
                source_scope=source_scope,
                scope_context=scope_context,
                now=now,
            )
            new_content = dump_document(metadata, body)
            validate_resulting_document(
                op.path,
                new_content,
                page_type=op.page_type,
                source_id=plan.source_id,
                source_scope=source_scope,
                scope_context=scope_context,
            )
            state[op.path] = new_content
            continue
        if op.op == "append_block":
            current = state.get(op.path, (root / op.path).read_text(encoding="utf-8"))
            if not op.content:
                raise ValueError(f"append_block requires content for {op.path}")
            current = current.rstrip() + "\n" + op.content.rstrip() + "\n"
            metadata, body = _merge_metadata(
                current,
                source_id=plan.source_id,
                source_scope=source_scope,
                scope_context=scope_context,
                now=now,
            )
            new_content = dump_document(metadata, body)
            validate_resulting_document(
                op.path,
                new_content,
                page_type=op.page_type,
                source_id=plan.source_id,
                source_scope=source_scope,
                scope_context=scope_context,
            )
            state[op.path] = new_content
            continue
        if op.op != "patch_sections":
            raise ValueError(f"Unsupported operation at apply stage: {op.op}")
        current = state.get(op.path, (root / op.path).read_text(encoding="utf-8"))
        parsed = parse_document(current)
        prefix, sections = _split_sections(parsed.body)
        section_map = {heading: content for heading, content in sections}
        section_order = [heading for heading, _ in sections]
        for patch in op.section_patches:
            if patch.action not in {"replace", "append", "prepend", "upsert_bullet"}:
                raise ValueError(f"Unsupported section action {patch.action}")
            if patch.section not in section_map:
                raise ValueError(f"Missing section {patch.section} in {op.path}")
            existing = section_map[patch.section]
            if patch.action == "replace":
                section_map[patch.section] = patch.content.rstrip() + "\n"
            elif patch.action == "append":
                section_map[patch.section] = existing.rstrip() + "\n" + patch.content.rstrip() + "\n"
            elif patch.action == "prepend":
                section_map[patch.section] = patch.content.rstrip() + "\n" + existing.lstrip("\n")
            else:
                if not patch.match_key:
                    raise ValueError("upsert_bullet requires match_key")
                section_map[patch.section] = _upsert_bullet(existing, patch.match_key, patch.content)
        rebuilt_body = _rebuild_sections(prefix, [(heading, section_map[heading]) for heading in section_order])
        metadata = parsed.metadata
        metadata["updated_at"] = now
        metadata["scope"] = scope_context.scope
        metadata["owner"] = scope_context.owner_or_null
        if "review_state" not in metadata:
            metadata["review_state"] = "unreviewed" if scope_context.scope == "shared" else "n/a"
        if "promotion_origin" not in metadata:
            metadata["promotion_origin"] = None
        source_ids = list(metadata.get("source_ids", []))
        if op.page_type != "changelog" and plan.source_id not in source_ids:
            source_ids.append(plan.source_id)
        metadata["source_ids"] = source_ids
        source_scopes = list(metadata.get("source_scope", []))
        if source_scope not in source_scopes:
            source_scopes.append(source_scope)
        metadata["source_scope"] = source_scopes
        new_content = dump_document(metadata, rebuilt_body)
        validate_resulting_document(
            op.path,
            new_content,
            page_type=op.page_type,
            source_id=plan.source_id,
            source_scope=source_scope,
            scope_context=scope_context,
        )
        state[op.path] = new_content
    return state


def changed_files(plan: RunPlan, state: dict[str, str], *, root: Path) -> dict[str, tuple[str, str]]:
    changed: dict[str, tuple[str, str]] = {}
    for path in plan.touched_paths:
        absolute = root / path
        old_content = absolute.read_text(encoding="utf-8") if absolute.exists() else ""
        new_content = state[path]
        if old_content != new_content:
            changed[path] = (old_content, new_content)
    return changed


def atomic_write_files(changed: dict[str, tuple[str, str]], *, root: Path) -> None:
    temp_paths: list[tuple[Path, Path]] = []
    try:
        for relative_path, (_, new_content) in changed.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_content)
                handle.flush()
                os.fsync(handle.fileno())
            temp_paths.append((temp_path, target))
        for temp_path, target in temp_paths:
            os.replace(temp_path, target)
    finally:
        for temp_path, _ in temp_paths:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)


def write_diff(job_id: str, *, changed: dict[str, tuple[str, str]], scoped_paths: ScopedPaths) -> Path:
    diff_path = scoped_paths.diff_path(job_id)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    for relative_path, (old_content, new_content) in changed.items():
        chunks.extend(
            unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=relative_path,
                tofile=relative_path,
            )
        )
    diff_path.write_text("".join(chunks), encoding="utf-8")
    return diff_path


def update_manifest(
    *,
    scoped_paths: ScopedPaths,
    source_id: str,
    checksum: str,
    source_page: str,
    affected_pages: list[str],
    job_id: str,
) -> Path:
    manifest_path = scoped_paths.manifest_path(source_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _validate_manifest_payload(
        {
        "source_id": source_id,
        "scope": scoped_paths.scope_context.scope,
        "owner": scoped_paths.scope_context.owner_or_null,
        "checksum": checksum,
        "source_page": source_page,
        "affected_pages": affected_pages,
        "last_job_id": job_id,
        "last_updated_at": utcnow_iso(),
        },
        scoped_paths=scoped_paths,
        source_id=source_id,
    )
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def write_run_record(
    *,
    scoped_paths: ScopedPaths,
    job_id: str,
    raw_model_output: str,
    plan: RunPlan | None = None,
    changed: dict[str, tuple[str, str]] | None = None,
    manifest_path: Path | None = None,
    failure: dict[str, Any] | None = None,
) -> Path:
    record_path = scoped_paths.run_record_path(job_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "scope": scoped_paths.scope_context.scope,
        "owner": scoped_paths.scope_context.owner_or_null,
        "raw_model_output": raw_model_output,
        "plan": None if plan is None else {
            "schema_version": plan.schema_version,
            "job_id": plan.job_id,
            "source_id": plan.source_id,
            "run_mode": plan.run_mode,
            "summary": plan.summary,
            "touched_paths": plan.touched_paths,
            "operations": [asdict(op) for op in plan.operations],
            "manifest_update": plan.manifest_update,
            "warnings": plan.warnings,
        },
        "failure": failure,
        "changed_files": [
            {
                "path": path,
                "old_sha256": sha256_text(old_content),
                "new_sha256": sha256_text(new_content),
            }
            for path, (old_content, new_content) in (changed or {}).items()
        ],
        "manifest_path": str(manifest_path) if manifest_path else None,
    }
    record_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record_path


def load_manifest(scoped_paths: ScopedPaths, source_id: str) -> dict[str, Any] | None:
    path = scoped_paths.manifest_path(source_id)
    if not path.exists():
        return None
    return _validate_manifest_payload(
        json.loads(path.read_text(encoding="utf-8")),
        scoped_paths=scoped_paths,
        source_id=source_id,
    )


def _load_scope_candidate_paths(scoped_paths: ScopedPaths, source_id: str | None, manifest: dict[str, Any] | None) -> set[str]:
    candidates = {
        scoped_paths.relative(scoped_paths.index_page_path()),
        scoped_paths.relative(scoped_paths.synthesis_page_path()),
        scoped_paths.relative(scoped_paths.changelog_page_path()),
    }
    if source_id is not None:
        candidates.add(scoped_paths.relative(scoped_paths.source_page_path(source_id)))
    if manifest:
        candidates.add(manifest["source_page"])
        candidates.update(manifest.get("affected_pages", []))
    return candidates


def load_shared_overlay_pages(root: Path) -> dict[str, str | None]:
    shared_paths = ScopedPaths(root, ScopeContext("shared"))
    candidates = _load_scope_candidate_paths(shared_paths, source_id=None, manifest=None)
    for manifest_path in sorted(shared_paths.manifests_root.glob("*.json")):
        source_id = manifest_path.stem
        manifest = _validate_manifest_payload(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            scoped_paths=shared_paths,
            source_id=source_id,
        )
        candidates.update(_load_scope_candidate_paths(shared_paths, source_id=source_id, manifest=manifest))
    output: dict[str, str | None] = {}
    for relative in sorted(candidates):
        absolute = root / relative
        output[relative] = absolute.read_text(encoding="utf-8") if absolute.exists() else None
    return output


def load_candidate_pages(scoped_paths: ScopedPaths, source_id: str, manifest: dict[str, Any] | None) -> dict[str, str | None]:
    candidates = _load_scope_candidate_paths(scoped_paths, source_id=source_id, manifest=manifest)
    output: dict[str, str | None] = {}
    for relative in sorted(candidates):
        absolute = scoped_paths.root / relative
        output[relative] = absolute.read_text(encoding="utf-8") if absolute.exists() else None
    return output


def derive_wiki_page_metadata(path: str, content: str) -> WikiPageMetadata:
    parsed = parse_document(content)
    sections = dict(_split_sections(parsed.body)[1])
    summary = sections.get("## One-line summary", "").strip().splitlines()
    return WikiPageMetadata(
        path=path,
        title=str(parsed.metadata["title"]),
        slug=str(parsed.metadata["slug"]),
        page_type=str(parsed.metadata["page_type"]),
        status=str(parsed.metadata["status"]),
        confidence=str(parsed.metadata["confidence"]),
        review_required=bool(parsed.metadata["review_required"]),
        source_ids=[str(item) for item in parsed.metadata.get("source_ids", [])],
        source_scope=[str(item) for item in parsed.metadata.get("source_scope", [])],
        scope=str(parsed.metadata["scope"]),
        owner=parsed.metadata.get("owner"),
        review_state=str(parsed.metadata["review_state"]),
        promotion_origin=parsed.metadata.get("promotion_origin"),
        summary=summary[0] if summary else "",
    )
