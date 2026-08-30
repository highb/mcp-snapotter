#!/usr/bin/env bash
# Download the OpenAPI spec from your SnapOtter instance.
# The catalog is built from it, so it must match the instance you point at.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=_python.sh
source ./_python.sh

eval "$("${SNAPOTTER_PY[@]}" -m snapotter_mcp.credentials --export)"
: "${SNAPOTTER_URL:?no SNAPOTTER_URL; see README Setup}"

args=(-sSf -m 60 -H "Authorization: Bearer ${SNAPOTTER_API_KEY:-}")
if [[ -n "${CF_ACCESS_CLIENT_ID:-}" && -n "${CF_ACCESS_CLIENT_SECRET:-}" ]]; then
  args+=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID"
         -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET")
fi

curl "${args[@]}" "${SNAPOTTER_URL%/}/api/v1/openapi.yaml" -o snapotter-openapi.yaml
printf 'wrote snapotter-openapi.yaml (%s bytes)\n' "$(stat -c%s snapotter-openapi.yaml)"
