from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol
from urllib import request


class Planner(Protocol):
    def plan(self, bundle: dict[str, Any]) -> str:
        ...


@dataclass
class OpenAICompatiblePlanner:
    api_key: str
    api_base: str
    model: str
    system_prompt: str

    def plan(self, bundle: dict[str, Any]) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(bundle, indent=2, sort_keys=True)},
            ],
            "temperature": 0,
        }
        req = request.Request(
            f"{self.api_base.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Planner returned no choices")
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
            return "".join(text_parts)
        if not isinstance(content, str):
            raise RuntimeError("Planner returned no text content")
        return content


@dataclass
class StaticPlanner:
    response: str

    def plan(self, bundle: dict[str, Any]) -> str:
        return self.response
