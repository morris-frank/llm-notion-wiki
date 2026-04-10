from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .models import ALLOWED_PAGE_TYPES, ScopeContext


INDEX_DIRNAME = "indexes"
SYNTHESIS_SEED_FILENAME = "current-state.md"
INDEX_SEED_FILENAME = "index.md"
CHANGELOG_SEED_FILENAME = "ingest-log.md"
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")


def safe_path_segment(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{label} must not be empty")
    if "/" in candidate or "\\" in candidate:
        raise ValueError(f"{label} must be a single path segment")
    if candidate in {".", ".."} or ".." in candidate:
        raise ValueError(f"{label} contains unsafe traversal")
    if not SAFE_SEGMENT_RE.fullmatch(candidate):
        raise ValueError(f"{label} contains unsupported characters")
    return candidate


@dataclass(frozen=True)
class ScopedPaths:
    root: Path
    scope_context: ScopeContext

    @property
    def owner_segment(self) -> str:
        if self.scope_context.owner is None:
            raise ValueError("Private scope requires owner segment")
        return safe_path_segment(self.scope_context.owner, label="owner")

    @property
    def raw_scope_root(self) -> Path:
        if self.scope_context.scope == "shared":
            return self.root / "raw" / "shared"
        return self.root / "raw" / "users" / self.owner_segment

    @property
    def raw_canonical_root(self) -> Path:
        return self.raw_scope_root / "canonical"

    @property
    def wiki_scope_root(self) -> Path:
        if self.scope_context.scope == "shared":
            return self.root / "wiki" / "shared"
        return self.root / "wiki" / "users" / self.owner_segment

    @property
    def manifests_root(self) -> Path:
        if self.scope_context.scope == "shared":
            return self.root / "state" / "manifests" / "shared"
        return self.root / "state" / "manifests" / "users" / self.owner_segment

    @property
    def runs_root(self) -> Path:
        if self.scope_context.scope == "shared":
            return self.root / "state" / "runs" / "shared"
        return self.root / "state" / "runs" / "users" / self.owner_segment

    @property
    def diffs_root(self) -> Path:
        if self.scope_context.scope == "shared":
            return self.root / "exports" / "diffs" / "shared"
        return self.root / "exports" / "diffs" / "users" / self.owner_segment

    @property
    def promotion_logs_root(self) -> Path:
        return self.root / "state" / "promotion_logs"

    def source_artifact_dir(self, source_id: str) -> Path:
        return self.raw_canonical_root / safe_path_segment(source_id, label="source_id")

    def source_artifact_dir_relative(self, source_id: str) -> str:
        return self.relative(self.source_artifact_dir(source_id))

    def page_dir(self, page_type: str) -> Path:
        mapping = {
            "source": self.wiki_scope_root / "sources",
            "concept": self.wiki_scope_root / "concepts",
            "synthesis": self.wiki_scope_root / "synthesis",
            "index": self.wiki_scope_root / INDEX_DIRNAME,
            "changelog": self.wiki_scope_root / INDEX_DIRNAME,
        }
        if page_type not in mapping:
            raise ValueError(f"Unsupported page type: {page_type}")
        return mapping[page_type]

    def page_path(self, page_type: str, filename: str) -> Path:
        return self.page_dir(page_type) / filename

    def source_page_path(self, source_id: str) -> Path:
        return self.page_path("source", f"{source_id}.md")

    def index_page_path(self) -> Path:
        return self.page_path("index", INDEX_SEED_FILENAME)

    def synthesis_page_path(self) -> Path:
        return self.page_path("synthesis", SYNTHESIS_SEED_FILENAME)

    def changelog_page_path(self) -> Path:
        return self.page_path("changelog", CHANGELOG_SEED_FILENAME)

    def manifest_path(self, source_id: str) -> Path:
        return self.manifests_root / f"{source_id}.json"

    def run_record_path(self, job_id: str) -> Path:
        return self.runs_root / f"{job_id}.json"

    def diff_path(self, job_id: str) -> Path:
        return self.diffs_root / f"{job_id}.patch"

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root))


def scope_root_directories(root: Path, owner: str | None = None) -> list[Path]:
    paths = [
        root / "raw" / "shared" / "inbox",
        root / "raw" / "shared" / "canonical",
        root / "raw" / "shared" / "archive",
        root / "wiki" / "shared" / "sources",
        root / "wiki" / "shared" / "concepts",
        root / "wiki" / "shared" / "synthesis",
        root / "wiki" / "shared" / INDEX_DIRNAME,
        root / "state" / "manifests" / "shared",
        root / "state" / "runs" / "shared",
        root / "exports" / "diffs" / "shared",
        root / "state" / "promotion_logs",
        root / "reviews" / "promotion_queue",
        root / "reviews" / "approved",
        root / "reviews" / "rejected",
        root / "config",
        root / "schema",
    ]
    if owner:
        owner_segment = safe_path_segment(owner, label="owner")
        paths.extend(
            [
                root / "raw" / "users" / owner_segment / "inbox",
                root / "raw" / "users" / owner_segment / "canonical",
                root / "raw" / "users" / owner_segment / "archive",
                root / "wiki" / "users" / owner_segment / "sources",
                root / "wiki" / "users" / owner_segment / "concepts",
                root / "wiki" / "users" / owner_segment / "synthesis",
                root / "wiki" / "users" / owner_segment / INDEX_DIRNAME,
                root / "state" / "manifests" / "users" / owner_segment,
                root / "state" / "runs" / "users" / owner_segment,
                root / "exports" / "diffs" / "users" / owner_segment,
            ]
        )
    return paths


def scope_path_prefix(scope_context: ScopeContext) -> str:
    if scope_context.scope == "shared":
        return "wiki/shared/"
    return f"wiki/users/{safe_path_segment(scope_context.owner or '', label='owner')}/"


def page_type_matches_path(path: str, page_type: str, scope_context: ScopeContext) -> bool:
    if page_type not in ALLOWED_PAGE_TYPES:
        return False
    prefix = scope_path_prefix(scope_context)
    if not path.startswith(prefix):
        return False
    if page_type == "source":
        return path.startswith(f"{prefix}sources/") and path.endswith(".md")
    if page_type == "concept":
        return path.startswith(f"{prefix}concepts/") and path.endswith(".md")
    if page_type == "synthesis":
        return path.startswith(f"{prefix}synthesis/") and path.endswith(".md")
    if page_type in {"index", "changelog"}:
        return path.startswith(f"{prefix}{INDEX_DIRNAME}/") and path.endswith(".md")
    return False
