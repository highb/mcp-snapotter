"""Runtime configuration.

Credentials come from whatever `snapotter.toml` configures -- 1Password
via the `op` CLI by default -- with environment variables able to override
any single value. See credentials.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .credentials import CredentialError, Settings, resolve


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


def _find_spec() -> Path:
    """Locate the OpenAPI spec: SNAPOTTER_SPEC, else alongside the repo root."""
    override = os.environ.get("SNAPOTTER_SPEC")
    if override:
        return Path(override).expanduser()
    # src/snapotter_mcp/config.py -> repo root is three parents up.
    root = Path(__file__).resolve().parents[2]
    for name in ("snapotter-openapi.yaml", "snapotter-api-1.json"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return root / "snapotter-openapi.yaml"


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    cf_client_id: str | None
    cf_client_secret: str | None
    spec_path: Path
    timeout: float
    async_timeout: float

    @classmethod
    def load(cls) -> Config:
        try:
            values = resolve()
            settings = Settings.load()
        except CredentialError as exc:
            raise ConfigError(str(exc)) from exc

        base_url = (values.get("url") or "").rstrip("/")
        api_key = values.get("api_key") or ""

        missing = [
            label
            for label, value in (("url", base_url), ("api_key", api_key))
            if not value
        ]
        if missing:
            where = (
                f"1Password item {settings.item!r}"
                if settings.provider == "1password"
                else "the environment"
            )
            raise ConfigError(
                f"could not resolve {', '.join(missing)} from {where} "
                f"(config: {settings.source}).\n"
                "Either export SNAPOTTER_URL and SNAPOTTER_API_KEY, or point "
                "snapotter.toml at a 1Password item. CF_ACCESS_CLIENT_ID and "
                "CF_ACCESS_CLIENT_SECRET are optional and only needed if your "
                "instance sits behind Cloudflare Access.\n"
                "Run `mise run creds` to see what currently resolves."
            )

        spec_path = _find_spec()
        if not spec_path.is_file():
            raise ConfigError(
                f"OpenAPI spec not found at {spec_path}. "
                "Run `mise run fetch-spec` to download it from your instance, "
                "or set SNAPOTTER_SPEC to its location."
            )

        return cls(
            base_url=base_url,
            api_key=api_key,
            cf_client_id=values.get("cf_client_id"),
            cf_client_secret=values.get("cf_client_secret"),
            spec_path=spec_path,
            timeout=float(os.environ.get("SNAPOTTER_TIMEOUT", "120")),
            async_timeout=float(os.environ.get("SNAPOTTER_ASYNC_TIMEOUT", "900")),
        )

    # Kept for callers that predate the credentials layer.
    from_env = load

    def headers(self) -> dict[str, str]:
        """Auth headers: SnapOtter API key plus Cloudflare Access service token."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # Both CF headers are required together; sending one alone does nothing.
        if self.cf_client_id and self.cf_client_secret:
            headers["CF-Access-Client-Id"] = self.cf_client_id
            headers["CF-Access-Client-Secret"] = self.cf_client_secret
        return headers
