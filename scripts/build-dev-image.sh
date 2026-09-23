#!/usr/bin/env bash
# Build a private development OCI image and record commit + digest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE_REPO="${HERMES_HELMET_IMAGE_REPO:-ghcr.io/machinewisdomai/hermes-helmet-oss}"
SOURCE_COMMIT="${HERMES_HELMET_SOURCE_COMMIT:-$(git rev-parse HEAD)}"
SHORT_COMMIT="$(printf '%s' "$SOURCE_COMMIT" | cut -c1-12)"
VERSION="${HERMES_HELMET_VERSION:-0.0.0-dev}"
TAG="${HERMES_HELMET_IMAGE_TAG:-dev-${SHORT_COMMIT}}"
FULL_REF="${IMAGE_REPO}:${TAG}"
BASE_IMAGE="${HERMES_BASE_IMAGE:-nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294}"
OUT_DIR="${HERMES_HELMET_DIST_DIR:-$ROOT/dist}"
IDENTITY_FILE="${OUT_DIR}/image-identity.json"
PUSH="${HERMES_HELMET_PUSH:-0}"

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
DIGEST=""

if [ "$PUSH" = "1" ]; then
  docker push "${FULL_REF}"
  DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "${FULL_REF}" || true)"
  if [ -z "$DIGEST" ] || [[ "$DIGEST" != *"@sha256:"* ]]; then
    if command -v docker >/dev/null 2>&1 && docker buildx imagetools inspect "${FULL_REF}" >/tmp/hermes-helmet-imagetools.txt 2>/dev/null; then
      remote_digest="$(awk '/Digest:/ {print $2; exit}' /tmp/hermes-helmet-imagetools.txt || true)"
      if [ -n "${remote_digest:-}" ]; then
        DIGEST="${IMAGE_REPO}@${remote_digest}"
      fi
    fi
  fi
fi

export IDENTITY_FILE FULL_REF IMAGE_REPO TAG IMAGE_ID DIGEST SOURCE_COMMIT VERSION BASE_IMAGE PUSH
python3 - <<'PY'
import json
import os
from pathlib import Path

identity = {
    "image_repository": os.environ["IMAGE_REPO"],
    "image_tag": os.environ["TAG"],
    "image_ref": os.environ["FULL_REF"],
    "image_id": os.environ["IMAGE_ID"],
    "image_digest": os.environ.get("DIGEST") or None,
    "source_commit": os.environ["SOURCE_COMMIT"],
    "version": os.environ["VERSION"],
    "base_image": os.environ["BASE_IMAGE"],
    "pushed": os.environ.get("PUSH") == "1",
}
path = Path(os.environ["IDENTITY_FILE"])
path.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
print(path.read_text(encoding="utf-8"))
PY

echo "Recorded image identity at ${IDENTITY_FILE}"
