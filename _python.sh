# Resolve a Python that has this package importable, without requiring mise.
# Sourced by the helper scripts. Preference order, cheapest first:
#   1. an already-activated virtualenv
#   2. a local .venv/
#   3. uv, if installed
#   4. mise (which supplies uv)
#   5. whatever python3 is on PATH
#
# Sets $SNAPOTTER_PY as an array; call it as: "${SNAPOTTER_PY[@]}" -m ...

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  SNAPOTTER_PY=("${VIRTUAL_ENV}/bin/python")
elif [[ -x .venv/bin/python ]]; then
  SNAPOTTER_PY=(.venv/bin/python)
elif command -v uv >/dev/null 2>&1; then
  SNAPOTTER_PY=(uv run --quiet python)
elif command -v mise >/dev/null 2>&1; then
  SNAPOTTER_PY=(mise exec -- uv run --quiet python)
elif command -v python3 >/dev/null 2>&1; then
  SNAPOTTER_PY=(python3)
else
  echo "error: no usable Python found (tried \$VIRTUAL_ENV, .venv, uv, mise, python3)" >&2
  exit 2
fi
