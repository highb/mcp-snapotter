"""End-to-end smoke test against the live SnapOtter instance.

Run with:  mise run smoke
Exercises discovery, a synchronous tool, a pipeline, and (if a video
fixture is available) the async job path.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from . import credentials, server
from .client import _json
from .config import Config


def _make_png(path: Path, width: int = 800, height: int = 600) -> Path:
    """Write a real PNG using only the stdlib, so the test needs no fixtures."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: none
        for x in range(width):
            raw += bytes(((x * 255) // width, (y * 255) // height, 128))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG (starts {data[:16]!r})")
    return struct.unpack(">II", data[16:24])


def _kind(path: Path) -> str:
    head = path.read_bytes()[:16]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:15].lower().startswith(b"<!doctype html"):
        return "HTML"
    return head[:8].hex()


def _purge(ids: list[str]) -> int:
    """Delete library files the smoke run created, chains and all."""
    if not ids:
        return 0
    response = _json(
        server.client()._http.request("DELETE", "/api/v1/files", json={"ids": ids})
    )
    return int(response.get("deleted", 0))


def main() -> int:
    failures: list[str] = []
    created: list[str] = []

    def check(label: str, fn: Callable[[], object]) -> object | None:
        try:
            result = fn()
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"  FAIL  {label}\n        {type(exc).__name__}: {exc}")
            return None
        print(f"  ok    {label}{'' if result is None else f' -> {result}'}")
        return result

    print("snapotter-mcp smoke test\n")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        os.environ.setdefault("SNAPOTTER_OUTPUT_DIR", str(tmpdir / "out"))
        source = _make_png(tmpdir / "input.png")

        print("configuration (offline)")

        def _env(**overrides: str | None) -> dict[str, str]:
            """Snapshot of os.environ with the credential vars forced."""
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in credentials.ENV_NAMES.values()
                and k not in (credentials.SERVICE_ACCOUNT_ENV, "SNAPOTTER_CONFIG")
            }
            env.update({k: v for k, v in overrides.items() if v is not None})
            return env

        def cfg_no_file() -> str:
            # find_config() searches the cwd, which during a smoke run is the
            # repo -- patch it out so this really tests "no config at all".
            with (
                mock.patch.object(credentials, "find_config", return_value=None),
                mock.patch.dict(
                    os.environ,
                    _env(SNAPOTTER_URL="https://ex.test", SNAPOTTER_API_KEY="si_x"),
                    clear=True,
                ),
            ):
                settings = credentials.Settings.load()
                values = credentials.resolve(settings)
            assert settings.provider == "env", settings.provider
            assert values["url"] == "https://ex.test", values.get("url")
            assert "cf_client_id" not in values, "CF should be absent"
            return "no config file -> env provider, no CF"

        def cfg_inferred() -> str:
            path = tmpdir / "inferred.toml"
            path.write_text('[secrets]\nitem = "abc"\n')
            assert credentials.Settings.load(path).provider == "1password"
            path.write_text("[instance]\n")
            assert credentials.Settings.load(path).provider == "env"
            return "provider inferred from whether an item is named"

        def cf_optional() -> str:
            empty = tmpdir / "empty.toml"
            empty.write_text("[instance]\n")
            base = {
                "SNAPOTTER_URL": "https://ex.test",
                "SNAPOTTER_API_KEY": "si_x",
                "SNAPOTTER_CONFIG": str(empty),
            }
            with mock.patch.dict(os.environ, _env(**base), clear=True):
                bare = Config.load().headers()
            with mock.patch.dict(
                os.environ,
                _env(
                    **base,
                    CF_ACCESS_CLIENT_ID="a.access",
                    CF_ACCESS_CLIENT_SECRET="cfast_x",
                ),
                clear=True,
            ):
                fronted = Config.load().headers()
            assert not any(k.startswith("CF-") for k in bare), sorted(bare)
            assert sum(k.startswith("CF-") for k in fronted) == 2, sorted(fronted)
            return "CF headers omitted when unset, sent when both present"

        check("env-only credentials", cfg_no_file)
        check("1Password is opt-in", cfg_inferred)
        check("Cloudflare Access is optional", cf_optional)

        print("\nconnectivity")
        check("health", lambda: server.snapotter_health()["health"])

        print("\ndiscovery")
        check(
            "list_tools",
            lambda: (
                f"{server.snapotter_list_tools()['total_tools']} tools "
                f"across {len(server.snapotter_list_tools()['sections'])} sections"
            ),
        )
        check(
            "describe image/resize",
            lambda: server.snapotter_describe_tool("image/resize")["endpoint"],
        )
        check(
            "unknown tool is rejected helpfully",
            lambda: _expect_error(
                lambda: server.snapotter_describe_tool("image/resiz")
            ),
        )

        print("\nsynchronous tool")

        def resize() -> str:
            out = server.snapotter_run_tool("image/resize", str(source), {"width": 200})
            path = Path(out["saved_to"])
            size = _png_size(path)
            assert size == (200, 150), f"expected 200x150, got {size[0]}x{size[1]}"
            return f"{size[0]}x{size[1]} {_kind(path)} ({out['bytes']} bytes)"

        check("resize 800x600 -> 200 wide", resize)

        print("\npipeline")

        def pipeline() -> str:
            out = server.snapotter_run_pipeline(
                str(source),
                [
                    {"toolId": "resize", "settings": {"width": 300}},
                    {"toolId": "convert", "settings": {"format": "webp"}},
                ],
            )
            path = Path(out["saved_to"])
            kind = _kind(path)
            assert kind == "webp", f"expected webp, got {kind}"
            return f"{out.get('stepsCompleted')} steps -> {kind} ({out['bytes']} bytes)"

        check("resize -> convert webp", pipeline)
        check(
            "bogus pipeline step is rejected",
            lambda: _expect_error(
                lambda: server.snapotter_run_pipeline(
                    str(source), [{"toolId": "color-effects"}]
                )
            ),
        )

        print("\nbatch")

        def batch() -> str:
            sources = [
                str(_make_png(tmpdir / f"batch{n}.png", 400, 300)) for n in range(3)
            ]
            out = server.snapotter_batch(
                "image/resize", sources, {"width": 100}, str(tmpdir / "batch-out")
            )
            assert out["count"] == 3, f"expected 3 results, got {out['count']}"
            for entry in out["results"]:
                size = _png_size(Path(entry["saved_to"]))
                assert size == (100, 75), f"expected 100x75, got {size}"
            return f"{out['count']}/{out['submitted']} files, all 100x75"

        check("resize 3 files via ZIP batch", batch)

        print("\nmulti-output (pages[] rather than one downloadUrl)")

        def multi_output() -> str:
            # Round-trip through PDF so the test needs no PDF fixture.
            as_pdf = server.snapotter_run_tool(
                "image/image-to-pdf", str(source), output_path=str(tmpdir / "rt.pdf")
            )
            pdf_path = as_pdf.get("saved_to") or as_pdf["files"][0]["saved_to"]
            out = server.snapotter_run_tool(
                "pdf/pdf-to-image",
                pdf_path,
                {"format": "png", "dpi": 72},
                str(tmpdir / "pages"),
            )
            assert out.get("count"), f"expected a page list, got keys {sorted(out)}"
            for entry in out["files"]:
                path = Path(entry["saved_to"])
                kind = _kind(path)
                assert kind == "png", f"{path.name} is {kind}, not png"
            return f"{out['count']} page(s), all real PNGs"

        check("pdf-to-image saves pages, not the zip", multi_output)

        print("\nfile library")

        def library() -> str:
            up = server.snapotter_upload_file(str(source))
            parent = up["id"]
            created.append(parent)
            out = server.snapotter_run_tool(
                "image/resize",
                str(source),
                {"width": 120},
                str(tmpdir / "lib.png"),
                save_as_version_of=parent,
            )
            lib = out["library"]
            assert lib["parentId"] == parent, "version not chained to its parent"
            assert lib["version"] == 2, f"expected version 2, got {lib['version']}"
            assert lib["toolChain"] == ["resize"], f"chain was {lib['toolChain']}"
            history = server.snapotter_get_file(parent).get("versions", [])
            assert len(history) >= 2, f"history has {len(history)} version(s)"
            return f"v1 -> v2, chain={lib['toolChain']}, {len(history)} versions"

        check("upload, save result as a new version", library)

        def library_multi_output_rejected() -> str:
            # Guarding this needs a genuinely multi-page result, so exercise
            # the guard directly rather than shipping a multi-page fixture.
            fake = {
                "count": 3,
                "files": [{"saved_to": f"/tmp/p{n}.png"} for n in range(3)],
            }
            return _expect_error(
                lambda: server._single_output(fake, "pdf/pdf-to-image")
            )

        check(
            "multi-output refuses an ambiguous library save",
            library_multi_output_rejected,
        )

        video = os.environ.get("SNAPOTTER_SMOKE_VIDEO")
        if video and Path(video).is_file():
            print("\nasync job")

            def compress() -> str:
                out = server.snapotter_run_tool(
                    "video/compress-video", video, {"crf": 30}
                )
                path = Path(out["saved_to"])
                kind = _kind(path)
                assert kind == "mp4", f"expected mp4, got {kind}"
                return f"{kind} {out['bytes']} bytes (from {out.get('originalSize')})"

            check("compress-video (202 -> SSE -> download)", compress)
        else:
            print(
                "\nasync job\n  skip  set SNAPOTTER_SMOKE_VIDEO to a video file to test"
            )

    if created:
        try:
            print(f"\ncleanup: removed {_purge(created)} library record(s)")
        except Exception as exc:
            print(f"\ncleanup: FAILED to remove {created}: {exc}")
            failures.append(f"cleanup: {exc}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


def _expect_error(fn: Callable[[], object]) -> str:
    try:
        fn()
    except Exception as exc:
        return f"rejected: {str(exc)[:80]}"
    raise AssertionError("expected an error, got success")


if __name__ == "__main__":
    sys.exit(main())
