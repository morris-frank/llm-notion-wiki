from __future__ import annotations

from dataclasses import dataclass
import json
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

