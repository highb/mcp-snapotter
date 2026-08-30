"""Parse the SnapOtter OpenAPI spec into a compact tool catalog.

The Tools surface is highly uniform: ~95% of the 259 tool endpoints are
POST /api/v1/tools/{section}/{toolId} taking a multipart `file` plus an
optional `settings` JSON string. That lets one generic handler cover
nearly everything, so we index the spec rather than generating 259
separate MCP tools (which would flood the model's context for no gain).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml


def _load_spec(path: Path) -> dict[str, Any]:
    """Read the OpenAPI document; SnapOtter serves YAML, files may be either."""
    text = path.read_text()
    if path.suffix in {".yaml", ".yml"}:
        loaded: dict[str, Any] = yaml.safe_load(text)
        return loaded
    parsed: dict[str, Any] = json.loads(text)
    return parsed


TOOL_PATH = re.compile(
    r"^/api/v1/tools/(?P<section>[a-z]+)/(?P<tool_id>[A-Za-z0-9._-]+)$"
)


@dataclass(frozen=True)
class ToolSpec:
    section: str
    tool_id: str
    summary: str
    description: str
    settings_doc: str
    file_fields: tuple[str, ...]
    other_fields: tuple[str, ...]
    accepts_settings: bool
    is_async: bool

    @property
    def name(self) -> str:
        return f"{self.section}/{self.tool_id}"

    @property
    def primary_file_field(self) -> str:
        return self.file_fields[0] if self.file_fields else "file"

    def brief(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.name, "summary": self.summary}
        if self.is_async:
            out["async"] = True
        if len(self.file_fields) > 1:
            out["files"] = list(self.file_fields)
        return out

    def detail(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "endpoint": f"POST /api/v1/tools/{self.section}/{self.tool_id}",
            "summary": self.summary,
            "description": self.description,
            "file_fields": list(self.file_fields),
            "accepts_settings": self.accepts_settings,
            "settings": self.settings_doc or "(no documented settings)",
            "other_fields": list(self.other_fields),
            "async": self.is_async,
            "notes": (
                "Async: returns 202 and completes over the job progress stream; "
                "run_tool waits for it automatically."
                if self.is_async
                else "Synchronous: returns a downloadUrl directly."
            ),
        }


def _is_binary(schema: dict[str, Any]) -> bool:
    """A field is an upload only if the spec says so.

    Every one of the 246 real binary fields declares format: binary, so
    matching on field *names* buys nothing and costs accuracy -- `fileId`
    is a scalar library id, not a file.
    """
    if schema.get("format") == "binary":
        return True
    items = schema.get("items")
    return isinstance(items, dict) and items.get("format") == "binary"


@dataclass
class Catalog:
    spec_path: Path
    _tools: dict[str, ToolSpec] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        spec = _load_spec(self.spec_path)
        for path, methods in spec.get("paths", {}).items():
            match = TOOL_PATH.match(path)
            if not match:
                continue
            op = methods.get("post")
            if not isinstance(op, dict):
                continue

            content = (op.get("requestBody") or {}).get("content") or {}
            form = (content.get("multipart/form-data") or {}).get("schema") or {}
            props = form.get("properties") or {}

            file_fields = tuple(
                n
                for n, s in props.items()
                if _is_binary(s if isinstance(s, dict) else {})
            )
            other = tuple(n for n in props if n not in file_fields and n != "settings")
            settings_schema = props.get("settings") or {}

            tool = ToolSpec(
                section=match["section"],
                tool_id=match["tool_id"],
                summary=op.get("summary", "") or "",
                description=(op.get("description", "") or "").strip(),
                settings_doc=(settings_schema.get("description", "") or "").strip(),
                file_fields=file_fields,
                other_fields=other,
                accepts_settings="settings" in props,
                is_async="202" in (op.get("responses") or {}),
            )
            self._tools[tool.name] = tool

    @cached_property
    def sections(self) -> list[str]:
        return sorted({t.section for t in self._tools.values()})

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, section: str, tool_id: str) -> ToolSpec | None:
        return self._tools.get(f"{section}/{tool_id}")

    def resolve(self, section: str, tool_id: str) -> ToolSpec:
        """Look up a tool, raising a message that helps the caller self-correct."""
        tool = self.get(section, tool_id)
        if tool:
            return tool
        near = self.search(tool_id, section=None, limit=5)
        hint = ""
        if near:
            hint = " Did you mean: " + ", ".join(t.name for t in near) + "?"
        raise KeyError(f"unknown tool '{section}/{tool_id}'.{hint}")

    def tools(self, section: str | None = None) -> list[ToolSpec]:
        """Every tool, or just one section's, sorted by name."""
        found = [
            tool
            for tool in self._tools.values()
            if section is None or tool.section == section
        ]
        return sorted(found, key=lambda t: (t.section, t.tool_id))

    def search(
        self, query: str, section: str | None = None, limit: int = 50
    ) -> list[ToolSpec]:
        q = query.lower().strip()
        if not q:
            return self.tools(section)[:limit]

        scored: list[tuple[int, ToolSpec]] = []
        for tool in self.tools(section):
            haystack = f"{tool.tool_id} {tool.summary} {tool.description}".lower()
            if q not in haystack:
                continue
            # Prefer id matches, then prefix matches, over description hits.
            score = 0 if tool.tool_id == q else 1 if tool.tool_id.startswith(q) else 2
            scored.append((score, tool))
        scored.sort(key=lambda pair: (pair[0], pair[1].name))
        return [tool for _, tool in scored[:limit]]
