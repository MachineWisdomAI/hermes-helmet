#!/bin/sh
# Give Claude Code the runtime subscription token without exposing it to the worker shell.
set -eu

token_file="${HERMES_HELMET_RUNTIME_DIR:-/run/hermes-helmet}/claude-code-oauth-token"
if [ -s "$token_file" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$(cat "$token_file")
    export CLAUDE_CODE_OAUTH_TOKEN
fi
# Without a token file, Claude Code falls back to its own login under CLAUDE_CONFIG_DIR.

upstream_claude=/usr/local/libexec/hermes-helmet/claude
if [ ! -x "$upstream_claude" ]; then
    echo "claude: pinned Claude Code binary is unavailable" >&2
    exit 1
fi
exec "$upstream_claude" "$@"
