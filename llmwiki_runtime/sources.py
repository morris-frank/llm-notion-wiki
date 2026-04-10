from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any
from urllib import request

from .models import SourceArtifacts, SourceRecord
from .notion import NotionClient, plain_text
from .wiki_ops import sha256_text


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


def _notion_rich_text_to_markdown(rich_text: list[dict[str, Any]] | None) -> str:
    return plain_text(rich_text)


def _block_to_markdown(block: dict[str, Any], indent: int = 0) -> str:
    block_type = block["type"]
    data = block.get(block_type, {})
    text = _notion_rich_text_to_markdown(data.get("rich_text"))
    prefix = "  " * indent
    if block_type == "paragraph":
        return f"{prefix}{text}\n\n" if text else "\n"
    if block_type == "heading_1":
        return f"# {text}\n\n"
    if block_type == "heading_2":
        return f"## {text}\n\n"
    if block_type == "heading_3":
        return f"### {text}\n\n"
    if block_type == "bulleted_list_item":
        return f"{prefix}- {text}\n"
    if block_type == "numbered_list_item":
        return f"{prefix}1. {text}\n"
    if block_type == "quote":
        return f"{prefix}> {text}\n\n"
    if block_type == "code":
        language = data.get("language", "text")
        return f"```{language}\n{text}\n```\n\n"
    if block_type == "to_do":
        checked = "x" if data.get("checked") else " "
        return f"{prefix}- [{checked}] {text}\n"
    return f"{prefix}{text}\n\n" if text else ""


def _collect_notion_blocks(client: NotionClient, block_id: str, indent: int = 0) -> str:
    markdown_chunks: list[str] = []
    cursor = None
    while True:
        response = client.retrieve_block_children(block_id, start_cursor=cursor)
        for result in response.get("results", []):
            markdown_chunks.append(_block_to_markdown(result, indent=indent))
            if result.get("has_children"):
                markdown_chunks.append(_collect_notion_blocks(client, result["id"], indent=indent + 1))
        cursor = response.get("next_cursor")
        if not response.get("has_more"):
            break
    return "".join(markdown_chunks)


@dataclass
class SourceFetcher:
    notion_client: NotionClient
    wiki_root: Path

    def fetch(self, source: SourceRecord) -> SourceArtifacts:
        output_dir = self.wiki_root / "raw" / "sources" / source.source_id
        if source.source_type == "web_page":
            return self._fetch_web_page(source, output_dir)
        if source.source_type == "notion_page":
            return self._fetch_notion_page(source, output_dir)
        raise ValueError(f"Unsupported source type: {source.source_type}")

    def _fetch_web_page(self, source: SourceRecord, output_dir: Path) -> SourceArtifacts:
        if not source.canonical_url:
            raise ValueError(f"web_page source is missing Canonical URL: {source.source_id}")
        with request.urlopen(source.canonical_url) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _HTMLTextExtractor()
        parser.feed(html)
        raw_text = parser.text()
        title = parser.title or source.title
        markdown = f"# {title}\n\n{raw_text}\n"
        metadata = {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "canonical_url": source.canonical_url,
            "title": title,
        }
        return _write_artifacts(output_dir, metadata, raw_text, markdown)

    def _fetch_notion_page(self, source: SourceRecord, output_dir: Path) -> SourceArtifacts:
        page = self.notion_client.retrieve_page(source.page_id)
        title = source.title or plain_text(page.get("properties", {}).get("title", {}).get("title"))
        markdown = _collect_notion_blocks(self.notion_client, source.page_id)
        markdown = f"# {title}\n\n{markdown.strip()}\n"
        raw_text = re.sub(r"(?m)^#+\s*", "", markdown)
        metadata = {
            "source_id": source.source_id,
            "source_type": source.source_type,
            "canonical_url": source.canonical_url,
            "title": title,
            "notion_page_id": source.page_id,
        }
        return _write_artifacts(output_dir, metadata, raw_text, markdown)
