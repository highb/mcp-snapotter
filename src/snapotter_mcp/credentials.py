"""Credential resolution: 1Password (via the `op` CLI) or the environment.

Configured by `snapotter.toml`. The 1Password path reads one item in a
single `op` invocation, so you get at most one biometric prompt and no
secrets on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "snapotter.toml"

# setting name -> environment variable, used by the "env" provider and as
# an override for any single value under any provider.
ENV_NAMES = {
    "url": "SNAPOTTER_URL",
    "api_key": "SNAPOTTER_API_KEY",
    "cf_client_id": "CF_ACCESS_CLIENT_ID",
    "cf_client_secret": "CF_ACCESS_CLIENT_SECRET",
}

DEFAULT_FIELDS = dict(ENV_NAMES)


class CredentialError(RuntimeError):
    """Raised when credentials cannot be resolved."""


def find_config() -> Path | None:
    """Locate snapotter.toml: $SNAPOTTER_CONFIG, cwd, then the package root."""
    override = os.environ.get("SNAPOTTER_CONFIG")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise CredentialError(f"SNAPOTTER_CONFIG points at a missing file: {path}")
        return path

    candidate = Path.cwd() / CONFIG_NAME
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class Settings:
    provider: str
    item: str | None
    account: str | None
    vault: str | None
    fields: dict[str, str]
    token_file: str | None
    url_override: str | None
    source: str

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path if path is not None else find_config()
        if path is None:
            # No config file: behave exactly as before, straight from the env.
            return cls(
                "env", None, None, None, dict(DEFAULT_FIELDS), None, None, "environment"
            )

        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise CredentialError(f"{path} is not valid TOML: {exc}") from exc

        secrets = data.get("secrets") or {}
        # No explicit provider: naming an item means 1Password, otherwise
        # plain environment variables.
        default_provider = "1password" if secrets.get("item") else "env"
        provider = str(secrets.get("provider", default_provider)).lower()
        if provider not in {"1password", "op", "env"}:
            raise CredentialError(
                f"{path}: unknown secrets.provider {provider!r}; "
                "expected '1password' or 'env'"
            )
        provider = "1password" if provider == "op" else provider

        fields = dict(DEFAULT_FIELDS)
        fields.update({k: str(v) for k, v in (secrets.get("fields") or {}).items()})

        return cls(
            provider=provider,
            item=secrets.get("item"),
            account=secrets.get("account"),
            vault=secrets.get("vault"),
            fields=fields,
            token_file=secrets.get("token_file"),
            url_override=(data.get("instance") or {}).get("url"),
            source=str(path),
        )


SERVICE_ACCOUNT_ENV = "OP_SERVICE_ACCOUNT_TOKEN"


def _read_dotenv(path: Path, key: str) -> str | None:
    """Pull one KEY=value out of a dotenv-style file. No expansion, no export."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise CredentialError(f"could not read token_file {path}: {exc}") from exc

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _op_env(settings: Settings) -> dict[str, str]:
    """Environment for the `op` subprocess, with the service account token.

    Only the bootstrap token lives on disk; the SnapOtter credentials it
    unlocks never touch the filesystem.
    """
    env = dict(os.environ)
    if env.get(SERVICE_ACCOUNT_ENV):
        return env
    if not settings.token_file:
        return env

    path = Path(settings.token_file).expanduser()
    if not path.is_absolute():
        path = Path(settings.source).parent / path
    if not path.is_file():
        raise CredentialError(
            f"{settings.source}: secrets.token_file points at a missing file: {path}"
        )

    mode = path.stat().st_mode & 0o077
    if mode:
        print(
            f"warning: {path} is readable beyond its owner (chmod 600 recommended)",
            file=sys.stderr,
        )

    token = _read_dotenv(path, SERVICE_ACCOUNT_ENV)
    if token:
        env[SERVICE_ACCOUNT_ENV] = token
    return env


def _op_item(settings: Settings) -> dict[str, str]:
    """Fetch the configured item and return {field label: value}."""
    if not shutil.which("op"):
        raise CredentialError(
            "the 1Password CLI (`op`) is not on PATH. Install it, or set "
            "secrets.provider = 'env' in snapotter.toml."
        )
    if not settings.item:
        raise CredentialError(
            f"{settings.source}: secrets.provider is '1password' but no "
            "secrets.item was given (use the item's UUID or title)."
        )

    cmd = ["op", "item", "get", settings.item, "--format", "json"]
    if settings.vault:
        cmd += ["--vault", settings.vault]
    if settings.account:
        cmd += ["--account", settings.account]

    env = _op_env(settings)
    if env.get(SERVICE_ACCOUNT_ENV) and not settings.vault:
        raise CredentialError(
            f"{settings.source}: a service account token is in use, so "
            "secrets.vault is required (service accounts cannot search "
            "across vaults)."
        )

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(
            "`op` timed out. If it is waiting on biometric unlock, run "
            "`op signin` once in this terminal first."
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit status {proc.returncode}"
        raise CredentialError(f"`op item get {settings.item}` failed: {message}")

    try:
        item = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CredentialError(f"`op` returned unparseable JSON: {exc}") from exc

    values: dict[str, str] = {}
    for field in item.get("fields") or []:
        value = field.get("value")
        if value is None:
            continue
        # Index by label and by id, so either can be named in the config.
        for key in (field.get("label"), field.get("id")):
            if key:
                values.setdefault(str(key), str(value))
    return values


def resolve(settings: Settings | None = None) -> dict[str, str]:
    """Return {url, api_key, cf_client_id, cf_client_secret}.

    Environment variables override any single resolved value, which keeps
    one-off testing easy without editing the config or 1Password.
    """
    settings = settings or Settings.load()

    resolved: dict[str, str] = {}
    if settings.provider == "1password":
        values = _op_item(settings)
        missing = []
        for name, label in settings.fields.items():
            if label in values:
                resolved[name] = values[label]
            elif name in {"url", "api_key"}:
                missing.append(label)
        if missing:
            raise CredentialError(
                f"1Password item {settings.item!r} has no field(s) named "
                f"{', '.join(missing)}. Present: {', '.join(sorted(values)) or 'none'}"
            )

    if settings.url_override:
        resolved["url"] = settings.url_override

    for name, env_name in ENV_NAMES.items():
        value = os.environ.get(env_name)
        if value:
            resolved[name] = value

    resolved.pop("", None)
    return {k: v.strip() for k, v in resolved.items() if v and v.strip()}


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    """Print shell exports for the resolved credentials.

    Intended for `eval "$(... --export)"`; it necessarily prints secrets.
    """
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--export", action="store_true", help="emit `export VAR=...` lines"
    )
    parser.add_argument(
        "--check", action="store_true", help="report what resolved, without values"
    )
    args = parser.parse_args()

    try:
        settings = Settings.load()
        values = resolve(settings)
    except CredentialError as exc:
        print(f"error: {exc}", flush=True)
        return 2

    if args.export:
        for name, env_name in ENV_NAMES.items():
            if name in values:
                print(f"export {env_name}={_shell_quote(values[name])}")
        return 0

    print(f"config   : {settings.source}")
    print(f"provider : {settings.provider}")
    if settings.provider == "1password":
        print(f"item     : {settings.item}")
    for name, env_name in ENV_NAMES.items():
        value = values.get(name)
        if not value:
            print(f"  {env_name:24s} MISSING")
        elif name == "url":
            print(f"  {env_name:24s} {value}")
        else:
            print(f"  {env_name:24s} set ({len(value)} chars)")
    return 0 if {"url", "api_key"} <= values.keys() else 1


if __name__ == "__main__":
    raise SystemExit(main())
