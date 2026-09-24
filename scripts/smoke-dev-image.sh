#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 IMAGE_REF" >&2
    exit 2
fi

image_ref="$1"
container_name="hermes-helmet-image-smoke-$$"
sentinel="helmet-smoke-sentinel-not-a-secret"

cleanup() {
    docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker run --detach \
    --name "$container_name" \
    --tmpfs /run/hermes-helmet:rw,uid=10000,gid=10000,mode=0700 \
    --env "GH_TOKEN=$sentinel" \
    --env "GITHUB_TOKEN=$sentinel" \
    --env HERMES_HELMET_POLICY_SOURCE=/opt/hermes-helmet/config/policy.example.json \
    "$image_ref" gateway run >/dev/null

attempt=0
while [ "$attempt" -lt 60 ]; do
    if docker exec --env GH_TOKEN= --env GITHUB_TOKEN= "$container_name" \
        sh -c 'test "$(basename "$(readlink /proc/1/exe)")" = "s6-svscan"' \
        && docker top "$container_name" -eo pid,args | grep -Fq 'hermes gateway run --replace'; then
        break
    fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" != "true" ]; then
        docker logs "$container_name" >&2
        echo "smoke: container exited before the s6 gateway became ready" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

if [ "$attempt" -eq 60 ]; then
    docker logs "$container_name" >&2
    echo "smoke: s6 gateway did not become ready" >&2
    exit 1
fi

docker exec --env GH_TOKEN= --env GITHUB_TOKEN= "$container_name" \
    /opt/hermes/.venv/bin/python -c '
import pathlib
import stat

needle = b"helmet-smoke-sentinel-not-a-secret"
store_hits = []
for path in pathlib.Path("/run/s6/container_environment").glob("*"):
    if needle in path.read_bytes():
        store_hits.append(str(path))

process_hits = []
for path in pathlib.Path("/proc").glob("[0-9]*/environ"):
    try:
        environment = path.read_bytes()
    except OSError:
        continue
    if needle in environment:
        process_hits.append(str(path))

token_file = pathlib.Path("/run/hermes-helmet/github-token")
token_ok = (
    token_file.is_file()
    and stat.S_IMODE(token_file.stat().st_mode) == 0o600
    and token_file.read_bytes() == needle
)
print({
    "s6_store_hits": store_hits,
    "process_environment_hits": process_hits,
    "runtime_token_file_ok": token_ok,
})
raise SystemExit(0 if token_ok and not store_hits and not process_hits else 1)
'

image_version="$(
    docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' \
        "$container_name"
)"
if [ "$image_version" != "0.1.0rc1" ]; then
    echo "smoke: expected org.opencontainers.image.version=0.1.0rc1, got ${image_version}" >&2
    exit 1
fi

# Report Hermes Agent version and confirm the Hermes Helmet package imports.
version_out="$(
    docker exec --env GH_TOKEN= --env GITHUB_TOKEN= "$container_name" \
        sh -c 'hermes --version && /opt/hermes/.venv/bin/python -c "import hermes_helmet; print(hermes_helmet.__name__)"'
)"
printf '%s\n' "$version_out"
case "$version_out" in
    *0.21.3*) ;;
    *)
        echo "smoke: expected Hermes Agent 0.21.3 in version output" >&2
        exit 1
        ;;
esac
case "$version_out" in
    *hermes_helmet*) ;;
    *)
        echo "smoke: expected hermes_helmet import" >&2
        exit 1
        ;;
esac

echo "Hermes Helmet image smoke passed."
