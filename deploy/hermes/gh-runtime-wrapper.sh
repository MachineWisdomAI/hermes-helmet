#!/bin/sh
# Give gh the runtime GitHub token without exposing it to the worker shell.
set -eu

token_file="${HERMES_HELMET_RUNTIME_DIR:-/run/hermes-helmet}/github-token"
if [ -s "$token_file" ]; then
    GH_TOKEN=$(cat "$token_file")
    export GH_TOKEN
    export GITHUB_TOKEN="$GH_TOKEN"
elif [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
    # Fallback for simple local runs that inject the token directly.
    export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"
    export GITHUB_TOKEN="$GH_TOKEN"
else
    echo "gh: runtime GitHub credential is unavailable" >&2
    exit 1
fi

upstream_gh=/usr/local/libexec/hermes-helmet/gh
if [ ! -x "$upstream_gh" ]; then
    echo "gh: pinned GitHub CLI binary is unavailable" >&2
    exit 1
fi
exec "$upstream_gh" "$@"
