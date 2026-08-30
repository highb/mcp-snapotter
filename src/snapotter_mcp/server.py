"""MCP server exposing the SnapOtter API.

SnapOtter has 243 tool routes, but they share one request shape, so this
exposes a handful of generic tools instead of 243 near-identical ones:
discovery (list/describe) plus execution (run/pipeline/batch) plus the
file library. The model discovers a tool's settings at call time, and
SnapOtter's validation errors name missing fields and valid enum values,
so a wrong first call is self-correcting.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import sys
import zipfile
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .catalog import Catalog
from .client import SnapOtterClient, SnapOtterError
from .config import Config, ConfigError

mcp = MCPServer(
    "snapotter",
    version=__version__,
    instructions=(
        "SnapOtter file processing: 243 tools across image, video, audio, pdf, files. "
        "Use snapotter_list_tools to find a tool, snapotter_describe_tool to learn its "
        "settings, then snapotter_run_tool. Chain steps with snapotter_run_pipeline."
    ),
)


@cache
def config() -> Config:
    return Config.load()


@cache
def catalog() -> Catalog:
    return Catalog(config().spec_path)


@cache
def client() -> SnapOtterClient:
    return SnapOtterClient(config())


def output_dir() -> Path:
    return Path(os.environ.get("SNAPOTTER_OUTPUT_DIR", "snapotter-output")).expanduser()


def surfaced[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Re-raise our errors as ToolError so the text reaches the model.

    The SDK replaces an uncaught exception with a bare "Error executing
    tool <name>". That would throw away SnapOtter's validation messages,
    which name the missing field and list valid enum values -- the thing
    that lets a wrong first call correct itself.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except KeyError as exc:  # catalog lookups carry their own message
            raise ToolError(str(exc.args[0]) if exc.args else str(exc)) from exc
        except (SnapOtterError, ValueError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _split(tool: str) -> tuple[str, str]:
    if "/" not in tool:
        raise SnapOtterError(
            f"tool must be 'section/tool_id' (e.g. 'image/resize'), got {tool!r}. "
            f"Sections: {', '.join(catalog().sections)}"
        )
    section, _, tool_id = tool.partition("/")
    return section, tool_id


def _settings_payload(settings: Any) -> dict[str, str]:
    """SnapOtter takes settings as a JSON *string* in a multipart field."""
    if settings is None or settings in ("", {}):
        return {}
    if isinstance(settings, str):
        try:
            json.loads(settings)
        except json.JSONDecodeError as exc:
            raise SnapOtterError(f"settings is not valid JSON: {exc}") from exc
        return {"settings": settings}
    return {"settings": json.dumps(settings)}


def _save(result: dict[str, Any], output_path: str | None) -> dict[str, Any]:
    """Download whatever the job produced and report where it landed.

    Some tools (pdf-to-image, image-to-pdf, gif-tools) return *many* outputs:
    a `pages` array of per-item URLs plus a zip of all of them under the
    top-level `downloadUrl`. Saving that `downloadUrl` to the caller's
    `output_path` would write a ZIP wearing a .png extension, so the pages
    array wins whenever it is present.
    """
    pages = result.get("pages")
    if isinstance(pages, list):
        urls = [
            p["downloadUrl"]
            for p in pages
            if isinstance(p, dict) and p.get("downloadUrl")
        ]
        if urls:
            return _save_pages(result, urls, output_path)

    url = result.get("downloadUrl")
    if not url:
        return result

    destination = (
        Path(output_path).expanduser() if output_path else output_dir() / Path(url).name
    )
    saved = client().download(url, destination)

    out = {
        "saved_to": str(saved.resolve()),
        "bytes": saved.stat().st_size,
        "jobId": result.get("jobId"),
    }
    for key in (
        "originalSize",
        "processedSize",
        "stepsCompleted",
        "steps",
        "savedFileId",
    ):
        if key in result:
            out[key] = result[key]
    if result.get("previewUrl"):
        out["previewUrl"] = result["previewUrl"]
    return out


def _save_pages(
    result: dict[str, Any], urls: list[str], output_path: str | None
) -> dict[str, Any]:
    """Save a multi-output result, one file per page."""
    if len(urls) == 1 and output_path and Path(output_path).suffix:
        # A single page and a concrete filename: honour it exactly.
        saved = client().download(urls[0], Path(output_path))
        files = [{"saved_to": str(saved.resolve()), "bytes": saved.stat().st_size}]
        target = saved.parent
    else:
        target = Path(output_path).expanduser() if output_path else output_dir()
        if target.suffix:  # a filename was given for a multi-page result
            target = target.with_suffix("")
        target.mkdir(parents=True, exist_ok=True)
        files = []
        for url in urls:
            saved = client().download(url, target / Path(url).name)
            files.append(
                {"saved_to": str(saved.resolve()), "bytes": saved.stat().st_size}
            )

    out = {
        "count": len(files),
        "output_dir": str(target.resolve()),
        "files": files,
        "jobId": result.get("jobId"),
    }
    for key in ("pageCount", "selectedPages", "format", "originalSize"):
        if key in result:
            out[key] = result[key]
    if result.get("zipUrl"):
        out["zip_available_at"] = result["zipUrl"]
    return out


def _single_output(result: dict[str, Any], what: str) -> Path:
    """The one produced file, or a clear error for multi-output tools."""
    if result.get("saved_to"):
        return Path(result["saved_to"])
    files = result.get("files") or []
    if len(files) == 1:
        return Path(files[0]["saved_to"])
    raise SnapOtterError(
        f"{what} produced {len(files)} files; saving a multi-output result to "
        "the library is ambiguous. Save one explicitly with snapotter_upload_file."
    )


def _save_to_library(path: Path, parent_id: str, tool_id: str) -> dict[str, Any]:
    """Attach a local result to a library file as its next version."""
    response = client().post_multipart(
        "/api/v1/files/save-result",
        files=[("file", path)],
        data={"parentId": parent_id, "toolId": tool_id},
    )
    entry = response.get("file") or response
    return {
        "id": entry.get("id"),
        "version": entry.get("version"),
        "parentId": entry.get("parentId"),
        "toolChain": entry.get("toolChain"),
    }


# --- discovery -----------------------------------------------------------


@mcp.tool()
@surfaced
def snapotter_list_tools(
    section: str | None = None, query: str | None = None, limit: int = 60
) -> dict[str, Any]:
    """List available SnapOtter tools.

    Args:
        section: restrict to one of image, video, audio, pdf, files.
        query: substring to match against tool id, summary, and description.
        limit: maximum tools to return.
    """
    cat = catalog()
    if section and section not in cat.sections:
        raise SnapOtterError(
            f"unknown section {section!r}; expected one of {', '.join(cat.sections)}"
        )
    tools = cat.search(query, section, limit) if query else cat.tools(section)[:limit]
    return {
        "sections": cat.sections,
        "total_tools": len(cat),
        "returned": len(tools),
        "tools": [t.brief() for t in tools],
    }


@mcp.tool()
@surfaced
def snapotter_describe_tool(tool: str) -> dict[str, Any]:
    """Describe one tool, including its accepted `settings` fields.

    Call this before snapotter_run_tool when unsure what settings a tool takes.

    Args:
        tool: "section/tool_id", e.g. "image/resize".
    """
    section, tool_id = _split(tool)
    return catalog().resolve(section, tool_id).detail()


# --- execution -----------------------------------------------------------


@mcp.tool()
@surfaced
def snapotter_run_tool(
    tool: str,
    file_path: str,
    settings: dict[str, Any] | str | None = None,
    output_path: str | None = None,
    extra_files: dict[str, str] | None = None,
    save_as_version_of: str | None = None,
) -> dict[str, Any]:
    """Run one SnapOtter tool on a local file and save the result locally.

    Async tools (most video work, some PDF) are awaited automatically.

    Args:
        tool: "section/tool_id", e.g. "image/resize" or "video/compress-video".
        file_path: local path to the input file.
        settings: tool options as an object; see snapotter_describe_tool.
        output_path: where to write the result; defaults to SNAPOTTER_OUTPUT_DIR.
        extra_files: additional binary fields for multi-input tools, mapping
            field name to local path (e.g. {"overlay": "logo.png"}).
        save_as_version_of: library file id (from snapotter_upload_file or
            snapotter_list_files). The result is stored as that file's next
            version, building a version chain.
    """
    section, tool_id = _split(tool)
    spec = catalog().resolve(section, tool_id)

    if settings and not spec.accepts_settings:
        raise SnapOtterError(f"{spec.name} accepts no settings")

    files = [(spec.primary_file_field, Path(file_path))]
    for field_name, path in (extra_files or {}).items():
        if field_name not in spec.file_fields:
            raise SnapOtterError(
                f"{spec.name} has no file field {field_name!r}; "
                f"it accepts: {', '.join(spec.file_fields)}"
            )
        files.append((field_name, Path(path)))

    data = _settings_payload(settings)
    # A few tools (pdf/sign-pdf) take `fileId` and save to the library
    # server-side, which avoids re-uploading the result. Everything else
    # goes through /files/save-result afterwards.
    native = save_as_version_of is not None and "fileId" in spec.other_fields
    if native and save_as_version_of is not None:
        data["fileId"] = save_as_version_of

    result = client().post_multipart(
        f"/api/v1/tools/{section}/{tool_id}",
        files=files,
        data=data,
    )
    saved = _save(result, output_path)

    if save_as_version_of:
        if native and result.get("savedFileId"):
            saved["library"] = {"id": result["savedFileId"], "via": "fileId"}
        else:
            saved["library"] = _save_to_library(
                _single_output(saved, spec.name), save_as_version_of, tool_id
            )
    return saved


@mcp.tool()
@surfaced
def snapotter_run_pipeline(
    file_path: str,
    steps: list[dict[str, Any]],
    output_path: str | None = None,
    save_as_version_of: str | None = None,
) -> dict[str, Any]:
    """Chain several tools over one file in a single server-side pass.

    Faster than sequential run_tool calls: intermediates never leave the server.

    Args:
        file_path: local path to the input file.
        steps: ordered list of {"toolId": "resize", "settings": {...}}. Use the
            bare tool id without its section.
        output_path: where to write the result.
        save_as_version_of: library file id to store the result under as a
            new version.
    """
    if not steps:
        raise SnapOtterError("steps must not be empty")

    known = {t.tool_id for t in catalog().tools()}
    for index, step in enumerate(steps, start=1):
        tool_id = step.get("toolId")
        if not tool_id:
            raise SnapOtterError(f"step {index} is missing 'toolId'")
        # The server advertises a few pipeline ids that have no endpoint and
        # hang the request rather than erroring; reject them up front.
        if tool_id not in known:
            raise SnapOtterError(
                f"step {index}: unknown toolId {tool_id!r}. "
                "Use snapotter_list_tools to find a valid id."
            )

    result = client().post_multipart(
        "/api/v1/pipeline/execute",
        files=[("file", Path(file_path))],
        data={"pipeline": json.dumps({"steps": steps})},
    )
    saved = _save(result, output_path)
    if save_as_version_of:
        chain = "+".join(str(step.get("toolId")) for step in steps)
        saved["library"] = _save_to_library(
            _single_output(saved, "pipeline"), save_as_version_of, chain
        )
    return saved


@mcp.tool()
@surfaced
def snapotter_batch(
    tool: str,
    file_paths: list[str],
    settings: dict[str, Any] | str | None = None,
    output_dir_path: str | None = None,
    keep_zip: bool = False,
) -> dict[str, Any]:
    """Run one tool over several files in a single request.

    The server answers with a ZIP archive, which is extracted for you.

    Args:
        tool: "section/tool_id".
        file_paths: local paths of the input files.
        settings: applied to every file.
        output_dir_path: directory for the extracted results; defaults to
            SNAPOTTER_OUTPUT_DIR.
        keep_zip: also keep the raw archive alongside the extracted files.
    """
    section, tool_id = _split(tool)
    spec = catalog().resolve(section, tool_id)
    if not file_paths:
        raise SnapOtterError("file_paths must not be empty")

    target = Path(output_dir_path).expanduser() if output_dir_path else output_dir()
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"{tool_id}-batch.zip"

    archive, headers = client().post_multipart_to_file(
        f"/api/v1/tools/{section}/{tool_id}/batch",
        files=[("file", Path(p)) for p in file_paths],
        destination=archive,
        data=_settings_payload(settings),
    )

    extracted = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if member.is_dir():
                    continue
                # Guard against path traversal in archive member names.
                name = Path(member.filename).name
                if not name:
                    continue
                out_path = target / name
                with bundle.open(member) as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(
                    {
                        "saved_to": str(out_path.resolve()),
                        "bytes": out_path.stat().st_size,
                    }
                )
    except zipfile.BadZipFile as exc:
        raise SnapOtterError(f"batch response was not a valid ZIP: {exc}") from exc
    finally:
        if not keep_zip:
            archive.unlink(missing_ok=True)

    result = {
        "tool": spec.name,
        "submitted": len(file_paths),
        "count": len(extracted),
        "output_dir": str(target.resolve()),
        "results": extracted,
    }
    if headers.get("x-job-id"):
        result["jobId"] = headers["x-job-id"]
    if keep_zip:
        result["archive"] = str(archive.resolve())
    return result


# --- library and diagnostics --------------------------------------------


@mcp.tool()
@surfaced
def snapotter_upload_file(file_path: str) -> dict[str, Any]:
    """Add a local file to the SnapOtter library.

    Returns its library id, which snapotter_run_tool accepts as
    `save_as_version_of` to build a version chain from it.
    """
    response = client().post_multipart(
        "/api/v1/files/upload", files=[("file", Path(file_path))]
    )
    # The upload endpoint answers with {"files": [...]}, even for one file.
    entries = response.get("files") or ([response] if response.get("id") else [])
    if not entries:
        raise SnapOtterError(f"upload returned no file record: {response}")
    return {
        "id": entries[0].get("id"),
        "name": entries[0].get("originalName"),
        "bytes": entries[0].get("size"),
        "version": entries[0].get("version"),
    }


@mcp.tool()
@surfaced
def snapotter_get_file(file_id: str) -> dict[str, Any]:
    """Get a library file's metadata and its version history."""
    return client().get_json(f"/api/v1/files/{file_id}")


@mcp.tool()
@surfaced
def snapotter_list_files(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List files saved in the SnapOtter library."""
    return client().get_json("/api/v1/files", {"limit": limit, "offset": offset})


@mcp.tool()
@surfaced
def snapotter_download(url_path: str, output_path: str) -> dict[str, Any]:
    """Download a SnapOtter URL (e.g. a downloadUrl) to a local path."""
    saved = client().download(url_path, Path(output_path))
    return {"saved_to": str(saved.resolve()), "bytes": saved.stat().st_size}


@mcp.tool()
@surfaced
def snapotter_health() -> dict[str, Any]:
    """Check that SnapOtter is reachable through Cloudflare Access."""
    return {
        "instance": config().base_url,
        "health": client().health(),
        "catalog_tools": len(catalog()),
    }


def main() -> None:
    # stdio is the MCP protocol channel, so keep library chatter on stderr
    # and quiet by default.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        config()
    except ConfigError as exc:
        raise SystemExit(f"snapotter-mcp: {exc}") from exc
    mcp.run()


if __name__ == "__main__":
    main()
