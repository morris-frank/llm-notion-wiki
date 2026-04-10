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

from .contracts import FILE_OPERATION_CONTRACT, MAINTAINER_CONTRACT, OPERATION_SCHEMA_VERSION, SEED_PAGES, wiki_root_paths
from .frontmatter import dump_document, parse_document
from .models import ALLOWED_OP_TYPES, ALLOWED_PAGE_TYPES, Operation, RunPlan, SectionPatch, WikiPageMetadata


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


def ensure_wiki_root(root: Path) -> None:
    for path in wiki_root_paths(root):
        path.mkdir(parents=True, exist_ok=True)
    agents_path = root / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(MAINTAINER_CONTRACT, encoding="utf-8")
    contract_path = root / "config" / "file-operation-contract.md"
    if not contract_path.exists():
        contract_path.write_text(FILE_OPERATION_CONTRACT, encoding="utf-8")
    for relative_path, page in SEED_PAGES.items():
        absolute = root / relative_path
        if absolute.exists():
            continue
        metadata = OrderedDict(
            [
                ("title", page["title"]),
                ("page_type", page["page_type"]),
                ("slug", page["slug"]),
                ("status", "draft"),
                ("updated_at", utcnow_iso()),
                ("source_ids", []),
                ("entity_keys", []),
                ("concept_keys", []),
                ("confidence", "medium"),
                ("review_required", False),
            ]
        )
        absolute.write_text(dump_document(metadata, page["body"]), encoding="utf-8")


def _validate_relative_wiki_path(path: str, page_type: str) -> None:
    if not path.startswith("wiki/"):
        raise ValueError(f"Path must stay under wiki/: {path}")
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"Unsafe path: {path}")
    if not path.endswith(".md"):
        raise ValueError(f"Wiki paths must end in .md: {path}")
    if page_type not in ALLOWED_PAGE_TYPES and page_type != "index":
        raise ValueError(f"Disallowed page type: {page_type}")
    allowed_prefixes = {
        "source": "wiki/sources/",
        "concept": "wiki/concepts/",
        "synthesis": "wiki/synthesis/",
        "changelog": "wiki/changelog/",
        "index": "wiki/index.md",
    }
    expected = allowed_prefixes.get(page_type)
    if expected is None:
        raise ValueError(f"Unknown page type {page_type}")
    if page_type == "index":
        if path != expected:
            raise ValueError("Index pages must use wiki/index.md")
        return
    if not path.startswith(expected):
        raise ValueError(f"Path {path} does not match page type {page_type}")


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


def validate_run_plan(plan: RunPlan, *, root: Path) -> None:
    if plan.schema_version != OPERATION_SCHEMA_VERSION:
        raise ValueError(f"Unexpected schema version: {plan.schema_version}")
    if plan.run_mode not in {"apply", "dry_run"}:
        raise ValueError(f"Unsupported run mode: {plan.run_mode}")
    if plan.summary.get("decision") == "no_op" and any(op.op != "no_op" for op in plan.operations):
        raise ValueError("no_op decision cannot include write operations")
    touched = {op.path for op in plan.operations if op.op != "no_op"}
    if touched != set(plan.touched_paths):
        raise ValueError("touched_paths must match non-no_op operations exactly")
    if not set(plan.touched_paths).issubset(set(plan.manifest_update.get("affected_pages", []))):
        raise ValueError("manifest_update.affected_pages must include all touched paths")
    if len(touched) > MAX_TOUCHED_FILES:
        raise ValueError(f"Plan touches too many files: {len(touched)}")
    total_patches = sum(len(op.section_patches) for op in plan.operations)
    if total_patches > MAX_SECTION_PATCHES:
        raise ValueError(f"Plan has too many section patches: {total_patches}")
    for op in plan.operations:
        _validate_relative_wiki_path(op.path, op.page_type)
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


def _merge_metadata(document_text: str, *, source_id: str, now: str) -> tuple[OrderedDict[str, object], str]:
    parsed = parse_document(document_text)
    metadata = parsed.metadata
    metadata["updated_at"] = now
    source_ids = list(metadata.get("source_ids", []))
    if source_id not in source_ids:
        source_ids.append(source_id)
    metadata["source_ids"] = source_ids
    return metadata, parsed.body


def _required_sections(page_type: str) -> list[str]:
    return SOURCE_PAGE_SECTIONS if page_type == "source" else GENERIC_PAGE_SECTIONS


def validate_resulting_document(path: str, content: str, *, page_type: str, source_id: str) -> None:
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
        "entity_keys",
        "concept_keys",
        "confidence",
        "review_required",
    ]
    for key in required_keys:
        if key not in metadata:
            raise ValueError(f"Missing required frontmatter key {key} in {path}")
    if metadata["page_type"] != page_type:
        raise ValueError(f"Frontmatter page_type mismatch in {path}")
    if page_type != "changelog" and source_id not in metadata.get("source_ids", []):
        raise ValueError(f"Current source_id missing from source_ids in {path}")
    for section in _required_sections(page_type):
        if section not in parsed.body:
            raise ValueError(f"Missing required section {section} in {path}")
    cited_sources = set(re.findall(r"\[S:([^\]]+)\]", parsed.body))
    sources_section_match = re.search(r"(?ms)^## Sources\n(?P<body>.*)$", parsed.body)
    sources_body = sources_section_match.group("body") if sources_section_match else ""
    for citation in cited_sources:
        if f"[S:{citation}]" not in sources_body:
            raise ValueError(f"Citation [S:{citation}] missing from ## Sources in {path}")


def apply_run_plan(plan: RunPlan, *, root: Path) -> dict[str, str]:
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
            metadata, body = _merge_metadata(op.content, source_id=plan.source_id, now=now)
            new_content = dump_document(metadata, body)
            validate_resulting_document(op.path, new_content, page_type=op.page_type, source_id=plan.source_id)
            state[op.path] = new_content
            continue
        if op.op == "append_block":
            current = state.get(op.path, (root / op.path).read_text(encoding="utf-8"))
            if not op.content:
                raise ValueError(f"append_block requires content for {op.path}")
            current = current.rstrip() + "\n" + op.content.rstrip() + "\n"
            metadata, body = _merge_metadata(current, source_id=plan.source_id, now=now)
            new_content = dump_document(metadata, body)
            validate_resulting_document(op.path, new_content, page_type=op.page_type, source_id=plan.source_id)
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
        source_ids = list(metadata.get("source_ids", []))
        if op.page_type != "changelog" and plan.source_id not in source_ids:
            source_ids.append(plan.source_id)
        metadata["source_ids"] = source_ids
        new_content = dump_document(metadata, rebuilt_body)
        validate_resulting_document(op.path, new_content, page_type=op.page_type, source_id=plan.source_id)
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


def write_diff(job_id: str, changed: dict[str, tuple[str, str]], *, root: Path) -> Path:
    diff_path = root / "exports" / "diffs" / f"{job_id}.patch"
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
    root: Path,
    source_id: str,
    checksum: str,
    source_page: str,
    affected_pages: list[str],
    job_id: str,
) -> Path:
    manifest_path = root / "state" / "manifests" / f"{source_id}.json"
    payload = {
        "source_id": source_id,
        "checksum": checksum,
        "source_page": source_page,
        "affected_pages": affected_pages,
        "last_job_id": job_id,
        "last_updated_at": utcnow_iso(),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def write_run_record(
    *,
    root: Path,
    job_id: str,
    raw_model_output: str,
    plan: RunPlan,
    changed: dict[str, tuple[str, str]],
    manifest_path: Path,
) -> Path:
    record_path = root / "state" / "runs" / f"{job_id}.json"
    payload = {
        "job_id": job_id,
        "raw_model_output": raw_model_output,
        "plan": {
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
        "changed_files": [
            {
                "path": path,
                "old_sha256": sha256_text(old_content),
                "new_sha256": sha256_text(new_content),
            }
            for path, (old_content, new_content) in changed.items()
        ],
        "manifest_path": str(manifest_path),
    }
    record_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record_path


def load_manifest(root: Path, source_id: str) -> dict[str, Any] | None:
    path = root / "state" / "manifests" / f"{source_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_candidate_pages(root: Path, source_id: str, manifest: dict[str, Any] | None) -> dict[str, str | None]:
    candidates = {
        f"wiki/sources/{source_id}.md",
        "wiki/index.md",
        "wiki/synthesis/current-state.md",
        "wiki/changelog/ingest-log.md",
    }
    if manifest:
        candidates.update(manifest.get("affected_pages", []))
    output: dict[str, str | None] = {}
    for relative in sorted(candidates):
        absolute = root / relative
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
        summary=summary[0] if summary else "",
    )
