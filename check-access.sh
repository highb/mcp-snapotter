#!/usr/bin/env bash
# Check whether the Cloudflare Access service token can reach SnapOtter.
#
#   ./check-access.sh
#
# Credentials come from snapotter.toml (1Password by default); environment
# variables override individual values. See `mise run creds`.
#
# Exit: 0 = reachable, 1 = blocked by Cloudflare Access, 2 = config/other error.

set -uo pipefail

cd "$(dirname "$0")" || exit 2

# shellcheck source=_python.sh
source ./_python.sh

# Resolved by the same code path the MCP server uses, so this can never
# disagree with the thing it is diagnosing.
if ! creds=$("${SNAPOTTER_PY[@]}" -m snapotter_mcp.credentials --export 2>&1); then
  echo "error: could not resolve credentials" >&2
  echo "$creds" >&2
  exit 2
fi
eval "$creds"

missing=()
for v in SNAPOTTER_URL CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  [[ -z "${!v:-}" ]] && missing+=("$v")
done
if (( ${#missing[@]} )); then
  echo "error: unresolved: ${missing[*]}" >&2
  echo "run: python -m snapotter_mcp.credentials --check" >&2
  exit 2
fi

URL="${SNAPOTTER_URL%/}/api/v1/health"
CF=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID"
    -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET")

echo "url    : $URL"
echo "id     : ${CF_ACCESS_CLIENT_ID:0:6}…${CF_ACCESS_CLIENT_ID: -8}  (len ${#CF_ACCESS_CLIENT_ID})"
echo "secret : ${CF_ACCESS_CLIENT_SECRET:0:6}…        (len ${#CF_ACCESS_CLIENT_SECRET})"
echo

headers=$(curl -sS -m 20 -D - -o /dev/null "${CF[@]}" "$URL" 2>/dev/null | tr -d '\r')
code=$(printf '%s' "$headers" | awk '/^HTTP/{c=$2} END{print c}')

case "$code" in
  200)
    echo "PASS  HTTP 200 — Cloudflare Access let the service token through."
    if [[ -n "${SNAPOTTER_API_KEY:-}" ]]; then
      body=$(curl -sS -m 20 "${CF[@]}" -H "Authorization: Bearer $SNAPOTTER_API_KEY" "$URL")
      echo "      SnapOtter says: $body"
    fi
    exit 0
    ;;
  302)
    echo "FAIL  HTTP 302 — Cloudflare Access redirected to login; the token was not accepted."
    meta=$(printf '%s' "$headers" | sed -n 's/^[Ll]ocation: //p' | grep -o 'meta=[^&]*' | cut -d= -f2)
    if [[ -n "$meta" ]]; then
      payload=$(printf '%s' "$meta" | cut -d. -f2 | tr '_-' '/+')
      payload="$payload$(printf '=%.0s' $(seq $(( (4 - ${#payload} % 4) % 4 ))))"
      decoded=$(printf '%s' "$payload" | base64 -d 2>/dev/null)
      if [[ -n "$decoded" ]] && command -v jq >/dev/null; then
        echo
        echo "      Access application AUD : $(printf '%s' "$decoded" | jq -r '.aud // "unknown"')"
        echo "      service_token_status   : $(printf '%s' "$decoded" \
          | jq -r 'if has("service_token_status") then (.service_token_status|tostring) else "unknown" end')"
        echo
        echo "      The Service Auth policy must be on the application with THAT AUD."
      fi
    fi
    exit 1
    ;;
  "")
    echo "FAIL  no response (timeout or DNS/TLS failure)."; exit 2 ;;
  *)
    echo "FAIL  HTTP $code — unexpected; not the usual Access redirect."; exit 2 ;;
esac
