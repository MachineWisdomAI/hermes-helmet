#!/usr/bin/env bash
# Owner-gated promotion: retag the already-scanned private candidate image as
# 0.1.0rc1 and print a dry-run private GitHub prerelease command
# (`gh release create`) for the same version. Uses docker buildx imagetools create
# against the existing digest. Never rebuilds. Never changes visibility.
# Refuses to run without an owner decision bound to evidence.
# CI never invokes this script. GitHub release creation stays dry-run unless
# an owner later sets HERMES_HELMET_PROMOTE_GITHUB=1 after HERMES_HELMET_PROMOTE=1.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DECISION="${HERMES_HELMET_OWNER_DECISION:-}"
if [ -z "$DECISION" ]; then
  echo "promote: HERMES_HELMET_OWNER_DECISION is required" >&2
  exit 1
fi
if [ ! -s "$DECISION" ]; then
  echo "promote: owner decision file is missing" >&2
  exit 1
fi

IMAGE_DIGEST="${HERMES_HELMET_IMAGE_DIGEST:-}"
if [ -z "$IMAGE_DIGEST" ]; then
  echo "promote: HERMES_HELMET_IMAGE_DIGEST must be the scanned immutable digest" >&2
  exit 1
fi

EVIDENCE="${HERMES_HELMET_EVIDENCE:-$ROOT/dist/rc/release-evidence.json}"
DIST="${HERMES_HELMET_DIST_DIR:-$ROOT/dist/rc}"
if [ ! -s "$EVIDENCE" ]; then
  echo "promote: candidate evidence is missing" >&2
  exit 1
fi

visibility="$(
  gh api \
    --header 'Accept: application/vnd.github+json' \
    --header 'X-GitHub-Api-Version: 2022-11-28' \
    "/orgs/${GITHUB_REPOSITORY_OWNER:-MachineWisdomAI}/packages/container/hermes-helmet" \
    --jq .visibility
)"
test "$visibility" = private

repo_visibility="$(
  gh api \
    --header 'Accept: application/vnd.github+json' \
    --header 'X-GitHub-Api-Version: 2022-11-28' \
    "/repos/${GITHUB_REPOSITORY:-MachineWisdomAI/hermes-helmet}" \
    --jq .visibility
)"
test "$repo_visibility" = private

COMMAND="$(
  PYTHONPATH=src python3 -m hermes_helmet.release_candidate promote-command \
    --decision "$DECISION" \
    --image-digest "$IMAGE_DIGEST" \
    --evidence "$EVIDENCE" \
    --dist "$DIST" \
    --dry-run
)"

echo "$COMMAND"
PYTHONPATH=src python3 -m hermes_helmet.release_candidate github-release-command \
  --decision "$DECISION" \
  --evidence "$EVIDENCE" \
  --dist "$DIST" \
  --dry-run
if [ "${HERMES_HELMET_PROMOTE:-0}" != "1" ]; then
  echo "promote: dry-run only; set HERMES_HELMET_PROMOTE=1 after the owner decision to retag"
  echo "promote: GitHub prerelease remains dry-run; do not mint 0.1.0"
  exit 0
fi

# Reuse the scanned manifest. Do not rebuild.
# shellcheck disable=SC2086
eval "$COMMAND"
echo "private 0.1.0rc1 tag applied to existing digest; visibility remains private"
if [ "${HERMES_HELMET_PROMOTE_GITHUB:-0}" != "1" ]; then
  echo "promote: GitHub prerelease dry-run only; printed gh release create above"
  exit 0
fi
# Execute structured argv from Python. Do not eval a printed command string.
PYTHONPATH=src python3 -m hermes_helmet.release_candidate github-release-command \
  --decision "$DECISION" \
  --evidence "$EVIDENCE" \
  --dist "$DIST"
echo "private v0.1.0rc1 GitHub prerelease path completed; visibility remains private"
