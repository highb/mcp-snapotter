"""HTTP client for SnapOtter, behind Cloudflare Access.

Two things here are load-bearing and easy to get wrong:

1. Cloudflare Access answers unauthenticated requests with its *sign-in page*
   at HTTP 200, so a status check alone will happily accept an HTML login
   form as a JPEG. Every response is sniffed for that before use.
2. Results are served via a CDN redirect, so downloads must follow redirects
   or you save the 302 body to disk instead of the file.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from .config import Config


class SnapOtterError(RuntimeError):
    """An API-level failure, carrying SnapOtter's own error text where possible."""


class AccessBlockedError(SnapOtterError):
    """Cloudflare Access rejected the request before SnapOtter ever saw it."""


def _looks_like_access_challenge(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return False
    if response.headers.get("www-authenticate", "").startswith("Cloudflare-Access"):
        return True
    head = response.content[:4096].decode("utf-8", "replace")
    return "Cloudflare Access" in head or "cloudflareaccess.com" in head


_ACCESS_HELP = (
    "Cloudflare Access blocked this request. The service token is missing, "
    "expired, or its Access policy action is 'Allow' rather than 'Service Auth'. "
    "Run ./check-access.sh to diagnose."
)


def _raise_for_error(response: httpx.Response) -> None:
    if _looks_like_access_challenge(response):
        raise AccessBlockedError(_ACCESS_HELP)
    if response.status_code < 400:
        return
    # SnapOtter's errors are JSON and unusually good: they name the missing
    # field and enumerate valid enum values, so pass them through verbatim.
    try:
        payload = response.json()
    except ValueError:
        detail = response.text[:400]
    else:
        detail = payload.get("error", json.dumps(payload))
        if payload.get("details"):
            detail = f"{detail} ({payload['details']})"
    raise SnapOtterError(f"HTTP {response.status_code}: {detail}")


def _is_json(response: httpx.Response) -> bool:
    return "application/json" in response.headers.get("content-type", "")


def _json(response: httpx.Response) -> dict[str, Any]:
    _raise_for_error(response)
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise SnapOtterError(
            f"expected JSON, got {response.headers.get('content-type', 'unknown')}"
        ) from exc
    return payload


@dataclass
class SnapOtterClient:
    config: Config

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            base_url=self.config.base_url,
            headers=self.config.headers(),
            timeout=self.config.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SnapOtterClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- plain API calls -------------------------------------------------

    def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return _json(self._http.get(path, params=params))

    def health(self) -> dict[str, Any]:
        return self.get_json("/api/v1/health")

    # --- tool execution --------------------------------------------------

    @contextmanager
    def _opened(
        self, files: Iterable[tuple[str, Path]]
    ) -> Iterator[Sequence[tuple[str, tuple[str, BinaryIO]]]]:
        """Open every upload, yield httpx's multipart payload, always close."""
        handles: list[Any] = []
        try:
            payload: list[tuple[str, tuple[str, BinaryIO]]] = []
            for field_name, file_path in files:
                resolved = Path(file_path).expanduser()
                if not resolved.is_file():
                    raise SnapOtterError(f"no such file: {resolved}")
                handle = resolved.open("rb")
                handles.append(handle)
                payload.append((field_name, (resolved.name, handle)))
            yield payload
        finally:
            for handle in handles:
                handle.close()

    def post_multipart(
        self,
        path: str,
        files: Iterable[tuple[str, Path]],
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._opened(files) as payload:
            response = self._http.post(path, files=payload, data=data or {})

        result = _json(response)
        if response.status_code == 202 or result.get("async"):
            job_id = result.get("jobId")
            if not job_id:
                raise SnapOtterError(f"async response without a jobId: {result}")
            return self.await_job(job_id)
        return result

    def await_job(self, job_id: str) -> dict[str, Any]:
        """Block on the SSE progress stream until the job reports a result."""
        last: dict[str, Any] = {}
        with self._http.stream(
            "GET",
            f"/api/v1/jobs/{job_id}/progress",
            timeout=httpx.Timeout(
                self.config.async_timeout, read=self.config.async_timeout
            ),
        ) as response:
            if _looks_like_access_challenge(response):
                raise AccessBlockedError(_ACCESS_HELP)
            if response.status_code >= 400:
                response.read()
                _raise_for_error(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                last = event
                if event.get("error"):
                    raise SnapOtterError(f"job {job_id} failed: {event['error']}")
                if event.get("result"):
                    return {**event["result"], "jobId": job_id}
                if event.get("phase") in {"failed", "cancelled"}:
                    raise SnapOtterError(f"job {job_id} ended as {event['phase']}")
        raise SnapOtterError(
            f"job {job_id} progress stream closed with no result "
            f"(last event: {last or 'none'})"
        )

    def post_multipart_to_file(
        self,
        path: str,
        files: Iterable[tuple[str, Path]],
        destination: Path,
        data: dict[str, str] | None = None,
    ) -> tuple[Path, dict[str, str]]:
        """POST a multipart upload whose *response* is a binary payload.

        The batch endpoint answers with application/zip rather than JSON.
        """
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            self._opened(files) as payload,
            self._http.stream("POST", path, files=payload, data=data or {}) as response,
        ):
            if response.status_code >= 400 or _is_json(response):
                response.read()
                _raise_for_error(response)
                raise SnapOtterError(
                    f"expected a binary payload, got JSON: {response.text[:300]}"
                )
            if _looks_like_access_challenge(response):
                raise AccessBlockedError(_ACCESS_HELP)
            with destination.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
            headers = dict(response.headers)
        return destination, headers

    # --- downloads -------------------------------------------------------

    def download(self, url_path: str, destination: Path) -> Path:
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._http.stream("GET", url_path) as response:
            if response.status_code >= 400:
                response.read()
                _raise_for_error(response)
            # Read the head before writing so an Access page never lands on disk
            # wearing a .mp4 extension.
            chunks = response.iter_bytes()
            head = next(chunks, b"")
            looks_html = (
                b"Cloudflare Access" in head[:4096] or b"<!DOCTYPE html>" in head[:64]
            )
            if looks_html and "text/html" in response.headers.get("content-type", ""):
                raise AccessBlockedError(_ACCESS_HELP)
            with destination.open("wb") as out:
                out.write(head)
                for chunk in chunks:
                    out.write(chunk)
        return destination
