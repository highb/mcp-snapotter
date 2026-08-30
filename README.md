# snapotter-mcp

An MCP server for [SnapOtter](https://github.com/snapotter-hq/SnapOtter) — 243 file-processing
tools across image, video, audio, PDF, and general files.

## Setup

Install however you like. With [mise](https://mise.jdx.dev):

```sh
mise install && mise exec -- uv sync
```

Or with nothing but Python 3.12+:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Either way you get a `snapotter-mcp` entry point. mise is a convenience,
not a requirement: nothing in the package imports it, and the helper scripts
fall back to any interpreter they can find.

Point it at your instance with two environment variables:

```sh
export SNAPOTTER_URL=https://snapotter.example.com
export SNAPOTTER_API_KEY=si_...        # Settings UI, or POST /api/v1/api-keys
```

That is the whole required setup. No config file is needed.

Then fetch the OpenAPI spec your instance serves — the tool catalog is built
from it, so it always matches the version you are talking to:

```sh
mise run fetch-spec
```

### Optional: Cloudflare Access

If your instance sits behind Cloudflare Access, set a service token and the
server adds the headers automatically:

```sh
export CF_ACCESS_CLIENT_ID=....access
export CF_ACCESS_CLIENT_SECRET=cfast_...
```

Leave them unset and requests go out plain. Nothing else changes. The Access
policy must use the **Service Auth** action — `Allow` with only a service-token
selector still demands an interactive login.

### Optional: 1Password instead of environment variables

Copy `snapotter.example.toml` to `snapotter.toml` and uncomment the `[secrets]`
block, naming the item that holds your credentials:

```toml
[secrets]
item  = "<1password item uuid>"   # or the item title
vault = "<vault uuid>"            # required with a service account token
token_file = ".env"               # holds OP_SERVICE_ACCOUNT_TOKEN
```

Naming an `item` is enough — the provider is inferred. Requires the `op` CLI.
With a service account token only that bootstrap token touches disk (keep it
`chmod 600`); the credentials it unlocks stay in memory. Omit `token_file` to
authenticate as a signed-in user through the desktop app instead.

Environment variables override any single resolved value under any provider,
so you can point at a different instance for one run without editing anything.
Other knobs: `SNAPOTTER_CONFIG`, `SNAPOTTER_OUTPUT_DIR` (default
`./snapotter-output`), `SNAPOTTER_SPEC`, `SNAPOTTER_TIMEOUT` (120s),
`SNAPOTTER_ASYNC_TIMEOUT` (900s).

mise tasks are thin wrappers; the plain equivalents work in any virtualenv:

| mise | plain |
|---|---|
| `mise run serve` | `snapotter-mcp` |
| `mise run creds` | `python -m snapotter_mcp.credentials --check` |
| `mise run check` | `./check-access.sh` |
| `mise run fetch-spec` | `./fetch-spec.sh` |
| `mise run smoke` | `python -m snapotter_mcp.smoke` |
| `mise run lint` | `ruff check src/ && ruff format --check src/` |
| `mise run types` | `mypy` |
| `mise run check-all` | all three of the above |

The two shell scripts pick an interpreter themselves — an active virtualenv,
then `.venv/`, then `uv`, then `mise`, then `python3` — so they need no
particular toolchain.

Both checkers run clean with no per-line suppressions. mypy is `strict = true`
over the whole package; the only concession is `ignore_missing_imports` for
`mcp.*`, which ships no stubs. Ruff runs pycodestyle, pyflakes, isort,
pyupgrade, bugbear, simplify, comprehensions, pathlib, return, unused-args and
a pylint subset. `max-args = 8` is raised from the default because an MCP
tool's signature *is* its public API.

Register with Claude Code via the included `.mcp.json`, or:

```sh
claude mcp add snapotter -- mise exec -- uv run snapotter-mcp
```

## Design

SnapOtter's 243 tool routes share one request shape — `POST
/api/v1/tools/{section}/{toolId}` with a multipart `file` plus an optional
`settings` JSON string. Rather than generate 243 near-identical MCP tools
(which would flood the model's context), this exposes eight generic ones and
indexes the OpenAPI spec for discovery:

| Tool | Purpose |
|---|---|
| `snapotter_list_tools` | Browse/search the catalog by section or keyword |
| `snapotter_describe_tool` | Show a tool's documented `settings` fields |
| `snapotter_run_tool` | Run one tool on a file; awaits async jobs |
| `snapotter_run_pipeline` | Chain steps server-side in one pass |
| `snapotter_batch` | One tool over many files |
| `snapotter_upload_file` | Add a local file to the library |
| `snapotter_list_files` | List the SnapOtter library |
| `snapotter_get_file` | One file's metadata and version history |
| `snapotter_download` | Fetch a `downloadUrl` to disk |
| `snapotter_health` | Connectivity check |

`snapotter_run_tool` and `snapotter_run_pipeline` take
`save_as_version_of=<library file id>` to store their result in the library as
a new version, building a version chain with a cumulative `toolChain`:

```
v1  image/png   153115B  900x900  []
v2  image/png    33886B  500x500  ['resize']
v3  image/png    30412B  520x520  ['resize', 'border']
v4  image/webp   31942B  520x520  ['resize', 'border', 'convert']
```

Pass the *previous version's* id to extend a chain; passing the same parent
repeatedly creates siblings at the same version number instead.

Settings are discovered at call time. SnapOtter's validation errors name the
missing field and enumerate valid enum values, so a wrong first call is
self-correcting — no need to hand-model 243 settings schemas.

## Things that bite

**Cloudflare Access returns its sign-in page at HTTP 200.** A status-code check
will cheerfully accept an HTML login form as a JPEG. The client sniffs every
response and raises `AccessBlockedError` instead. This is also why
`check-access.sh` parses the body rather than trusting `%{http_code}`.

**Results are served through a CDN redirect.** Downloads must follow redirects
or you save the 302 body to disk.

**54 of 243 tools are async** (most video, some PDF): they return `202
{"async": true}` with no `downloadUrl`, and complete over the SSE stream at
`/api/v1/jobs/{jobId}/progress`. `snapotter_run_tool` handles this
transparently.

**Batch answers with `application/zip`, not JSON.** `POST
/api/v1/tools/{section}/{toolId}/batch` streams back an archive of results
with the job id in an `X-Job-Id` header. `snapotter_batch` saves it, extracts
the members (flattening names, so a hostile archive can't traverse out of the
output directory), and deletes the archive unless `keep_zip=true`.

**Some tools return many outputs, not one.** `pdf/pdf-to-image`,
`image/image-to-pdf`, and `image/gif-tools` answer with a `pages[]` array of
per-item URLs *plus* a zip of everything under the top-level `downloadUrl`.
Saving that `downloadUrl` writes a ZIP wearing whatever extension you asked
for. `run_tool` prefers `pages[]` whenever present: one page with a concrete
filename is honoured exactly, several pages go into a directory.

**`GET /api/v1/pipeline/tools` over-advertises.** It lists `color-effects`,
`brightness-contrast`, `color-channels`, and `saturation`, which have no
endpoint; `color-effects` *hangs* a pipeline rather than erroring.
`snapotter_run_pipeline` validates step ids against the spec first.

**`fileId` is not a general mechanism.** `ToolResponse.savedFileId` reads as
though any tool can auto-save to the library, but only `pdf/sign-pdf`
declares a `fileId` form field, and passing it to other tools is silently
ignored — no error, no `savedFileId`. The general path is
`POST /api/v1/files/save-result` with `parentId` + `toolId`, which is what
`save_as_version_of` uses; `fileId` is passed natively only where the tool
actually supports it.

**Tool errors must be re-raised as `ToolError`.** The MCP SDK replaces an
uncaught exception with a bare "Error executing tool <name>", discarding
SnapOtter's validation text. The `@surfaced` decorator converts our
exceptions so the model sees `format: Invalid enum value. Expected 'jpg' |
'png' | ...` and can correct itself.

**Downloads have no app-level auth.** `/api/v1/download/{jobId}/{filename}`
needs no SnapOtter API key — only Cloudflare Access protects it. Don't add an
Access bypass for `/api/v1/*` unless you accept that jobId URLs become public.

## License

MIT — see [LICENSE](LICENSE).

SnapOtter itself is AGPL-3.0. This is an independent client that talks to it
over HTTP, so the two licenses are unrelated. The OpenAPI spec is fetched from
your own instance at setup rather than vendored here.
