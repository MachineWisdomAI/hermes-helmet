#!/bin/sh
# Bridge the runtime GitHub token into gh without exposing it to worker shells.
# Always clear raw PAT env vars before exec'ing Hermes, and never stay root.
set -eu

export HOME="${HOME:-/opt/data}"
export HERMES_HOME="${HERMES_HOME:-/opt/data}"

uid="${HERMES_UID:-10000}"
gid="${HERMES_GID:-10000}"
runtime_dir="${HERMES_HELMET_RUNTIME_DIR:-/run/hermes-helmet}"
token_file="$runtime_dir/github-token"
worker_home="$HERMES_HOME/home"
policy_target="/opt/data/github-issue-poller/policy.json"

install_runtime_dirs() {
    if [ "$(id -u)" -eq 0 ]; then
        install -d -m 700 -o "$uid" -g "$gid" "$runtime_dir" "$worker_home" || {
            mkdir -p "$runtime_dir" "$worker_home"
            chown "$uid:$gid" "$runtime_dir" "$worker_home"
            chmod 700 "$runtime_dir" "$worker_home"
        }
        return
    fi
    mkdir -p "$runtime_dir" "$worker_home"
    chmod 700 "$runtime_dir" "$worker_home" 2>/dev/null || true
}

if [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    token="${GH_TOKEN:-$GITHUB_TOKEN}"
    install_runtime_dirs
    umask 077
    token_tmp="$runtime_dir/.github-token.$$"
    trap 'rm -f "$token_tmp"' EXIT HUP INT TERM
    printf '%s' "$token" > "$token_tmp"
    mv "$token_tmp" "$token_file"
    trap - EXIT HUP INT TERM
    chmod 600 "$token_file" 2>/dev/null || true
    if [ "$(id -u)" -eq 0 ]; then
        chown "$uid:$gid" "$token_file" 2>/dev/null || true
    fi

    if command -v git >/dev/null 2>&1; then
        HOME="$worker_home" git config --global --unset-all credential.helper 2>/dev/null || true
        HOME="$worker_home" git config --global --replace-all \
            credential.https://github.com.helper '!gh auth git-credential'
        if [ "$(id -u)" -eq 0 ]; then
            chown -R "$uid:$gid" "$worker_home" 2>/dev/null || true
        fi
    fi
fi

if [ -n "${HERMES_HELMET_POLICY_SOURCE:-}" ] && [ -f "$HERMES_HELMET_POLICY_SOURCE" ]; then
    policy_dir=$(dirname "$policy_target")
    if [ "$(id -u)" -eq 0 ]; then
        install -d -m 700 -o "$uid" -g "$gid" "$policy_dir"
        install -m 600 -o "$uid" -g "$gid" \
            "$HERMES_HELMET_POLICY_SOURCE" "$policy_target"
    else
        mkdir -p "$policy_dir"
        install -m 600 "$HERMES_HELMET_POLICY_SOURCE" "$policy_target"
    fi
fi

# Preserve the file-backed gh wrapper; never leave the raw PAT in Hermes env.
unset GH_TOKEN || true
unset GITHUB_TOKEN || true

# Optional company skill-pack import on the supported startup path. Skip/reject
# must never block upstream Hermes. Runs only after raw PAT env is cleared.
run_company_skills_import() {
    if [ "${HERMES_HELMET_SKIP_COMPANY_SKILLS:-0}" = "1" ]; then
        echo "company skill import: status=skipped message=disabled by HERMES_HELMET_SKIP_COMPANY_SKILLS" || true
        return 0
    fi

    policy_file=""
    if [ -n "${HERMES_HELMET_POLICY_SOURCE:-}" ] && [ -f "$HERMES_HELMET_POLICY_SOURCE" ]; then
        policy_file="$HERMES_HELMET_POLICY_SOURCE"
    elif [ -f "$policy_target" ]; then
        policy_file="$policy_target"
    fi

    if [ -z "$policy_file" ]; then
        echo "company skill import: status=skipped message=company skill pack is not configured" || true
        return 0
    fi

    py="${HERMES_HELMET_PYTHON:-}"
    if [ -z "$py" ]; then
        if [ -x /opt/hermes/.venv/bin/python ]; then
            py=/opt/hermes/.venv/bin/python
        elif command -v python3 >/dev/null 2>&1; then
            py=$(command -v python3)
        else
            echo "company skill import: status=skipped message=python unavailable" || true
            return 0
        fi
    fi

    helmet_src="${HERMES_HELMET_SRC:-/opt/hermes-helmet/src}"
    if [ ! -d "$helmet_src" ]; then
        echo "company skill import: status=skipped message=hermes-helmet source unavailable" || true
        return 0
    fi

    export PYTHONPATH="${helmet_src}${PYTHONPATH:+:$PYTHONPATH}"
    # Non-blocking: any failure prints skipped/rejected and returns success to caller.
    if ! "$py" -m hermes_helmet.cli import-company-skills --config "$policy_file" 2>&1; then
        echo "company skill import: status=skipped message=import hook failed" || true
    fi
    return 0
}
# shellcheck disable=SC2310
run_company_skills_import || true

# Start the upstream s6 chain only after raw PAT variables are gone. /init
# snapshots its environment for supervised services, including the dashboard;
# entering it earlier would leak the worker token into those process trees.
# The upstream main wrapper preserves normal command routing and drops root to
# the image's hermes user when required.
exec /init /opt/hermes/docker/main-wrapper.sh "$@"
