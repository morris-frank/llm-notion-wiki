from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _require(env: dict[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _optional_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    notion_token: str
    notion_version: str
    notion_api_base: str
    control_db_id: str | None
    sources_data_source_id: str
    wiki_data_source_id: str
    jobs_data_source_id: str
    policies_data_source_id: str
    wiki_root: Path
    worker_name: str
    poll_interval_seconds: int
    admin_api_key: str | None
    llm_api_key: str | None
    llm_api_base: str
    llm_model: str | None
    notion_webhook_signing_secret: str | None
    notion_webhook_verification_token: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        data = dict(os.environ if env is None else env)
        return cls(
            notion_token=_require(data, "NOTION_TOKEN"),
            notion_version=data.get("NOTION_VERSION", "2026-03-11"),
            notion_api_base=data.get("NOTION_API_BASE", data.get("API_BASE", "https://api.notion.com/v1")),
            control_db_id=data.get("CONTROL_DB_ID"),
            sources_data_source_id=_require(data, "SOURCES_DS_ID"),
            wiki_data_source_id=_require(data, "WIKI_DS_ID"),
            jobs_data_source_id=_require(data, "JOBS_DS_ID"),
            policies_data_source_id=_require(data, "POLICIES_DS_ID"),
            wiki_root=Path(data.get("WIKI_ROOT", "./llmwiki")).resolve(),
            worker_name=data.get("WORKER_NAME", "llmwiki-runtime"),
            poll_interval_seconds=_optional_int(data, "POLL_INTERVAL_SECONDS", 5),
            admin_api_key=data.get("ADMIN_API_KEY"),
            llm_api_key=data.get("OPENAI_API_KEY", data.get("LLM_API_KEY")),
            llm_api_base=data.get("OPENAI_BASE_URL", data.get("LLM_API_BASE", "https://api.openai.com/v1")),
            llm_model=data.get("OPENAI_MODEL", data.get("LLM_MODEL")),
            notion_webhook_signing_secret=data.get("NOTION_WEBHOOK_SIGNING_SECRET"),
            notion_webhook_verification_token=data.get("NOTION_WEBHOOK_VERIFICATION_TOKEN"),
        )
