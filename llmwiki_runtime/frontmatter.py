from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


FRONTMATTER_START = "---"


@dataclass
class ParsedDocument:
    metadata: OrderedDict[str, object]
    body: str


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value == "[]":
        return []
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_document(text: str) -> ParsedDocument:
    if not text.startswith(f"{FRONTMATTER_START}\n"):
        raise ValueError("Document is missing frontmatter start delimiter")
    parts = text.split(f"\n{FRONTMATTER_START}\n", 1)
    if len(parts) != 2:
        raise ValueError("Document is missing frontmatter end delimiter")
    frontmatter_block = parts[0][len(FRONTMATTER_START) + 1 :]
    body = parts[1]
    metadata: OrderedDict[str, object] = OrderedDict()
    current_key: str | None = None
    current_list: list[object] | None = None
    for raw_line in frontmatter_block.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("  - "):
            if current_key is None or current_list is None:
                raise ValueError("List entry without active key")
            current_list.append(_parse_scalar(line[4:]))
            continue
        current_key = None
        current_list = None
        if ":" not in line:
            raise ValueError(f"Invalid frontmatter line: {line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value == "":
            metadata[key] = []
            current_key = key
            current_list = metadata[key]
        else:
            metadata[key] = _parse_scalar(raw_value)
    return ParsedDocument(metadata=metadata, body=body)


def _format_scalar(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def dump_document(metadata: OrderedDict[str, object], body: str) -> str:
    lines = [FRONTMATTER_START]
    for key, value in metadata.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_format_scalar(item)}")
            continue
        lines.append(f"{key}: {_format_scalar(value)}")
    lines.append(FRONTMATTER_START)
    return "\n".join(lines) + "\n" + body.lstrip("\n")
