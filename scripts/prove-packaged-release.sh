#!/bin/sh
# Install the packaged wheel outside PYTHONPATH=src and prove:
# hermes-helmet setup, hermes-helmet doctor, hermes-helmet status,
# install-skills (setup-helmet, helmet-issue, helmet-epic), company path, quickstart.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DIST="${HERMES_HELMET_DIST_DIR:-$ROOT/dist/rc}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CREATED_WORK=0
if [ -n "${HERMES_HELMET_PROOF_WORK:-}" ]; then
  WORK="$(
    PYTHONPATH=src python3 -m hermes_helmet.release_candidate prepare-dir \
      --path "$HERMES_HELMET_PROOF_WORK"
  )"
  CREATED_WORK=1
else
  WORK="$(mktemp -d)"
  CREATED_WORK=1
fi
cleanup() {
  if [ -z "${HERMES_HELMET_PROOF_KEEP:-}" ] && [ "$CREATED_WORK" = 1 ]; then
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

WHEEL="$(find "$DIST" -name 'hermes_helmet-*.whl' | head -n 1)"
if [ -z "$WHEEL" ]; then
  echo "prove: wheel missing in $DIST" >&2
  exit 1
fi

"$PYTHON_BIN" -m venv "$WORK/venv"
VENV_PY="$WORK/venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install --no-deps "$WHEEL"
CONSOLE="$WORK/venv/bin/hermes-helmet"

# Force empty PYTHONPATH so checkout src/ cannot shadow the wheel.
export PYTHONPATH=""
"$VENV_PY" "$ROOT/scripts/prove-packaged-release.py" \
  --console "$CONSOLE" \
  --fixture-root "$ROOT" \
  --work "$WORK/proof"

echo "packaged hermes-helmet setup, doctor, status, install-skills, company path, and quickstart passed"
