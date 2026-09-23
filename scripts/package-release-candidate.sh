#!/usr/bin/env bash
# Package the private v0.1.0rc1 CLI, bundled skills, checksums, and documents.
# Does not mint a numbered v0.1.0 image tag or change visibility.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SOURCE_COMMIT="${HERMES_HELMET_SOURCE_COMMIT:-$(git rev-parse HEAD)}"
IMAGE_DIGEST="${HERMES_HELMET_IMAGE_DIGEST:-}"
OUT="$(
  PYTHONPATH=src python3 -m hermes_helmet.release_candidate prepare-dir \
    --path "${HERMES_HELMET_DIST_DIR:-$ROOT/dist/rc}" \
    --root "$ROOT"
)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -x "${ROOT}/.venv-rc/bin/python" ]; then
  "$PYTHON_BIN" -m venv "${ROOT}/.venv-rc"
fi
RC_PYTHON="${ROOT}/.venv-rc/bin/python"
"$RC_PYTHON" -m pip install --upgrade pip setuptools wheel build >/dev/null
"$RC_PYTHON" -m pip wheel --no-deps --no-build-isolation -w "$OUT" "$ROOT"
"$RC_PYTHON" -m build --sdist --outdir "$OUT" "$ROOT"

cp LICENSE NOTICE CHANGELOG.md "$OUT/"
if [ -n "${HERMES_HELMET_SBOM:-}" ]; then
  cp "$HERMES_HELMET_SBOM" "$OUT/source-sbom.spdx.json"
elif [ -f "$ROOT/source-sbom.spdx.json" ]; then
  cp "$ROOT/source-sbom.spdx.json" "$OUT/source-sbom.spdx.json"
fi

if [ -n "$IMAGE_DIGEST" ]; then
  PYTHONPATH=src "$PYTHON_BIN" -m hermes_helmet.release_candidate write \
    --source-revision "$SOURCE_COMMIT" \
    --image-digest "$IMAGE_DIGEST" \
    --dist "$OUT" \
    --out "$OUT/release-evidence.json"
else
  PYTHONPATH=src "$PYTHON_BIN" - <<PY
from pathlib import Path
from hermes_helmet.release_candidate import collect_artifacts, write_checksums
dist = Path("$OUT")
write_checksums(dist, collect_artifacts(dist))
print("package artifacts and SHA256SUMS written; image digest pending scanned dev manifest")
PY
fi

echo "private RC package written to ${OUT}"
