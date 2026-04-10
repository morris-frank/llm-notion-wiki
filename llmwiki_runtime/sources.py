from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
from typing import Any
from urllib import request
from urllib.parse import urlparse

from .models import SourceArtifacts, SourceRecord
from .notion import NotionClient, notion_page_id_from_reference
from .paths import ScopedPaths
from .wiki_ops import sha256_text


def _reject_if_non_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError("URL host resolves to a non-public address")


def assert_public_http_url(url: str) -> str:
    """Reject non-http(s) schemes and hosts that resolve to non-public addresses (SSRF mitigation)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _reject_if_non_public_ip(literal)
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host {host!r}") from e
    checked_public = 0
    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        _reject_if_non_public_ip(resolved)
        checked_public += 1
    if checked_public == 0:
        raise ValueError("URL host did not resolve to a public address")
    return url


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        self._chunks.append(text)

    def text(self) -> str:
        text = " ".join(self._chunks)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _write_artifacts(base_dir: Path, metadata: dict[str, Any], raw_text: str, markdown: str) -> SourceArtifacts:
    base_dir.mkdir(parents=True, exist_ok=True)
    checksum = f"sha256:{sha256_text(markdown)}"
    metadata_path = base_dir / "metadata.json"
    text_path = base_dir / "source.txt"
    markdown_path = base_dir / "source.md"
    metadata = dict(metadata)
    metadata["checksum"] = checksum
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(raw_text, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return SourceArtifacts(
        metadata=metadata,
        raw_text=raw_text,
        markdown=markdown,
        checksum=checksum,
        storage_dir=base_dir,
    )


@dataclass
class SourceFetcher:
    notion_client: NotionClient
    wiki_root: Path

    def fetch(self, source: SourceRecord) -> SourceArtifacts:
        output_dir = ScopedPaths(self.wiki_root, source.scope_context).source_artifact_dir(source.source_id)
        if source.source_type == "web_page":
            return self._fetch_web_page(source, output_dir)
        if source.source_type == "notion_page":
            return self._fetch_notion_page(source, output_dir)
        raise ValueError(f"Unsupported source type: {source.source_type}")

    def _fetch_web_page(self, source: SourceRecord, output_dir: Path) -> SourceArtifacts:
        if not source.canonical_url:
            raise ValueError(f"web_page source is missing Canonical URL: {source.source_id}")
        fetch_url = assert_public_http_url(source.canonical_url)
        req = request.Request(
            fetch_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; llmwiki-runtime/0.1; +https://example.invalid)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with request.urlopen(req) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _HTMLTextExtractor()
        parser.feed(html)
        raw_text = parser.text()
        title = parser.title or source.title
        markdown = f"# {title}\n\n{raw_text}\n"
        metadata = {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "scope": source.scope,
            "owner": source.owner,
            "canonical_url": source.canonical_url,
            "title": title,
        }
        return _write_artifacts(output_dir, metadata, raw_text, markdown)

    def _fetch_notion_page(self, source: SourceRecord, output_dir: Path) -> SourceArtifacts:
        target_page_id = source.target_page_id or notion_page_id_from_reference(source.canonical_url)
        if not target_page_id:
            raise ValueError(
                f"notion_page source is missing a target page reference: {source.source_id}. "
                "Set Target Notion Page ID or Canonical URL to the target page."
            )
        title = source.title
        if hasattr(self.notion_client, "page_markdown"):
            markdown = self.notion_client.page_markdown(target_page_id, title=title)
        else:  # pragma: no cover - compatibility for older test doubles
            page = self.notion_client.retrieve_page(target_page_id)
            body = ""
            if hasattr(self.notion_client, "retrieve_block_children"):
                from .notion import _collect_notion_blocks

                body = _collect_notion_blocks(self.notion_client, target_page_id).strip()
            fallback_title = title or page.get("id", "Untitled")
            markdown = f"# {fallback_title}\n\n{body}\n"
        raw_text = re.sub(r"(?m)^#+\s*", "", markdown)
        metadata = {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "scope": source.scope,
            "owner": source.owner,
            "canonical_url": source.canonical_url,
            "title": title,
            "notion_page_id": target_page_id,
        }
        return _write_artifacts(output_dir, metadata, raw_text, markdown)
