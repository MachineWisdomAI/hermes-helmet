#!/usr/bin/env bash
# Build a local development OCI image and record its source identity.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE_REPO="${HERMES_HELMET_IMAGE_REPO:-hermes-helmet}"
SOURCE_COMMIT="${HERMES_HELMET_SOURCE_COMMIT:-$(git rev-parse HEAD)}"
SHORT_COMMIT="$(printf '%s' "$SOURCE_COMMIT" | cut -c1-12)"
VERSION="${HERMES_HELMET_VERSION:-0.0.0-dev}"
TAG="${HERMES_HELMET_IMAGE_TAG:-dev-${SHORT_COMMIT}}"
FULL_REF="${IMAGE_REPO}:${TAG}"
BASE_IMAGE="${HERMES_BASE_IMAGE:-nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294}"
OUT_DIR="${HERMES_HELMET_DIST_DIR:-$ROOT/dist}"
IDENTITY_FILE="${OUT_DIR}/image-identity.json"

mkdir -p "$OUT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to build the development image" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not available" >&2
  exit 1
fi

echo "Building ${FULL_REF} from ${SOURCE_COMMIT}"
docker build \
  --file deploy/Dockerfile \
  --build-arg "HERMES_BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "HERMES_HELMET_SOURCE_COMMIT=${SOURCE_COMMIT}" \
  --build-arg "HERMES_HELMET_VERSION=${VERSION}" \
  --label "org.opencontainers.image.revision=${SOURCE_COMMIT}" \
  --tag "${FULL_REF}" \
  .

IMAGE_ID="$(docker inspect --format='{{.Id}}' "${FULL_REF}")"
export IDENTITY_FILE FULL_REF IMAGE_REPO TAG IMAGE_ID SOURCE_COMMIT VERSION BASE_IMAGE
python3 - <<'PY'
import json
import os
from pathlib import Path

identity = {
    "image_repository": os.environ["IMAGE_REPO"],
    "image_tag": os.environ["TAG"],
    "image_ref": os.environ["FULL_REF"],
    "image_id": os.environ["IMAGE_ID"],
    "image_digest": None,
    "source_commit": os.environ["SOURCE_COMMIT"],
    "version": os.environ["VERSION"],
    "base_image": os.environ["BASE_IMAGE"],
    "pushed": False,
}
path = Path(os.environ["IDENTITY_FILE"])
path.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
print(path.read_text(encoding="utf-8"))
PY

echo "Recorded image identity at ${IDENTITY_FILE}"
