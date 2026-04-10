from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib import error, parse, request


class NotionAPIError(RuntimeError):
    pass


@dataclass
class NotionClient:
    token: str
    version: str
    api_base: str

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.api_base.rstrip('/')}{path}",
            data=data,
            headers=self._headers(),
            method=method,
        )
        try:
            with request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotionAPIError(f"{method} {path} failed with {exc.code}: {detail}") from exc

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/pages",
            {
                "parent": {"data_source_id": data_source_id},
                "properties": properties,
            },
        )

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/data_sources/{data_source_id}")

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_obj: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int = 100,
        start_cursor: str | None = None,
        filter_properties: list[str] | None = None,
    ) -> dict[str, Any]:
        query_params = ""
        if filter_properties:
            encoded = parse.urlencode([("filter_properties[]", value) for value in filter_properties])
            query_params = f"?{encoded}"
        payload: dict[str, Any] = {"page_size": page_size}
        if filter_obj:
            payload["filter"] = filter_obj
        if sorts:
            payload["sorts"] = sorts
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", f"/data_sources/{data_source_id}/query{query_params}", payload)

    def retrieve_block_children(self, block_id: str, start_cursor: str | None = None) -> dict[str, Any]:
        suffix = ""
        if start_cursor:
            suffix = f"?start_cursor={parse.quote(start_cursor)}"
        return self._request("GET", f"/blocks/{block_id}/children{suffix}")

    def page_markdown(self, page_id: str, *, title: str | None = None) -> str:
        page = self.retrieve_page(page_id)
        resolved_title = title or plain_text(page.get("properties", {}).get("title", {}).get("title")) or "Untitled"
        body = _collect_notion_blocks(self, page_id).strip()
        if body:
            return f"# {resolved_title}\n\n{body}\n"
        return f"# {resolved_title}\n"


def title_property(value: str) -> dict[str, Any]:
    return {"title": [{"text": {"content": value}}]}


def rich_text_property(value: str) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": value}}]}


def select_property(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def checkbox_property(value: bool) -> dict[str, Any]:
    return {"checkbox": value}


def number_property(value: int | float | None) -> dict[str, Any]:
    return {"number": value}


def url_property(value: str | None) -> dict[str, Any]:
    return {"url": value}


def date_property(value: str | None) -> dict[str, Any]:
    return {"date": {"start": value}} if value else {"date": None}


def relation_property(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids]}


def plain_text(rich_nodes: list[dict[str, Any]] | None) -> str:
    if not rich_nodes:
        return ""
    return "".join(node.get("plain_text", "") for node in rich_nodes)


def multi_select_names(options: list[dict[str, Any]] | None) -> list[str]:
    if not options:
        return []
    return [str(option.get("name")) for option in options if option.get("name")]


_NOTION_ID_RE = re.compile(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")


def normalize_notion_id(value: str) -> str | None:
    cleaned = value.strip()
    match = _NOTION_ID_RE.fullmatch(cleaned)
    if not match:
        return None
    hex_value = cleaned.replace("-", "").lower()
    return (
        f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
        f"{hex_value[16:20]}-{hex_value[20:32]}"
    )


def notion_page_id_from_reference(value: str | None) -> str | None:
    if not value:
        return None
    direct = normalize_notion_id(value)
    if direct:
        return direct
    parsed = parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    match = None
    for candidate in reversed(parsed.path.split("/")):
        found = _NOTION_ID_RE.search(candidate)
        if found:
            match = found.group(1)
            break
    if not match:
        return None
    return normalize_notion_id(match)


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
