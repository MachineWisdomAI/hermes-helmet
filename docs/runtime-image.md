# Published worker image

The Hermes Helmet worker runs from a Docker image. The host `helmet` CLI and
Captain skills are a separate installation: `pip install` does not pull GHCR
and does not start or restart the worker.

## Current preview pin

Use the digest, not a mutable tag:

```text
ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea
```

| Field | Value |
| --- | --- |
| Tag | `preview-c0fc5d883b56befcf1bd8354c6dab7c612ac6455` |
| Source commit | `c0fc5d883b56befcf1bd8354c6dab7c612ac6455` |
| Platforms | `linux/amd64`, `linux/arm64` |
| Build record | [GitHub Actions run 36228558544](https://github.com/MachineWisdomAI/hermes-helmet/actions/runs/36228558544) |

Anonymous pulls work. This image is a preview. Its `0.1.0rc1` package label is
the version declared in that source commit; it is not a new stable release and
it is not the [September 23, 2026 wheel](https://github.com/MachineWisdomAI/hermes-helmet/releases/tag/preview-2026-09-23).

The digest identifies the image built from that recorded source SHA.
`/opt/hermes-helmet/SOURCE_COMMIT` inside the container is that SHA.

## Run the published image

From a clone of this repository, after `deploy/.env` holds the worker token and
`HERMES_HELMET_IMAGE` is set to the digest above. Compose interpolates `image:`
from that file. A leftover shell export of `HERMES_HELMET_IMAGE` overrides
`.env`; `docker pull "$HERMES_HELMET_IMAGE"` does not read `.env`. If this
shell previously exported the variable, unset it so `.env` wins, then pull and
start through Compose:

```sh
unset HERMES_HELMET_IMAGE
docker compose --env-file deploy/.env -f deploy/compose.yaml pull hermes
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --no-build
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  cat /opt/hermes-helmet/SOURCE_COMMIT
```

`--no-build` is the normal runtime path. Compose still knows how to build from
`deploy/Dockerfile`; that path is for development, below.

`deploy/compose.yaml` defaults `image` to `hermes-helmet:local` when
`HERMES_HELMET_IMAGE` is unset in both the shell and `deploy/.env`. Leave that
default for local builds, or set `HERMES_HELMET_IMAGE=hermes-helmet:local`
explicitly. Do not point it at `latest` or `main`.

The image entrypoint writes the worker token to an owner-only file on tmpfs,
copies the mounted policy into `/opt/data/github-issue-poller/policy.json`,
removes `GH_TOKEN` and `GITHUB_TOKEN`, then starts upstream Hermes. See the
[quickstart](quickstart.md) for checkout, profile login, and poller install,
and [private-overlay.md](private-overlay.md) to keep company configuration in a
separate repository.

## Development build

To wrap the same `deploy/Dockerfile` around a local checkout, select
`hermes-helmet:local` explicitly. Unsetting the shell variable is not enough
when `deploy/.env` still pins the published digest:

```sh
export HERMES_HELMET_SOURCE_COMMIT="$(git rev-parse HEAD)"
export HERMES_HELMET_IMAGE=hermes-helmet:local
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  cat /opt/hermes-helmet/SOURCE_COMMIT
```

`SOURCE_COMMIT` should match `git rev-parse HEAD` of the tree you built.
`scripts/build-dev-image.sh` and `scripts/smoke-dev-image.sh` are the same
development path without Compose.

The Dockerfile still pins the upstream Hermes Agent base
`nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294`.
Override only with another immutable tag+digest.

## Change the pin or roll back

Pause new intake before replacing the image. Removing the policy
`dispatch_label` from open issues stops new issue dispatch; it does not pause
the review-repair poller. Use the Hermes cron pause/resume interface on the
existing `github-issue-poller` job. Leave the worker running so queued or
running tasks can finish or be explicitly reconciled. Recording a blocker does
not cancel a task.

1. Discover the job, then pause it:

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
     /opt/hermes/.venv/bin/hermes cron list
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
     /opt/hermes/.venv/bin/hermes cron pause github-issue-poller
   ```

2. Leave the worker running. Let queued and running implementation or repair
   tasks finish, or reconcile them explicitly. Keep the named volume and the
   existing issue/task/PR associations.

3. Set `HERMES_HELMET_IMAGE` in `deploy/.env` to the new digest (or the
   previous digest to roll back). If this shell still exports an older value,
   unset it so `.env` wins:

   ```sh
   unset HERMES_HELMET_IMAGE
   docker compose --env-file deploy/.env -f deploy/compose.yaml pull hermes
   docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --no-build
   ```

4. Confirm `/opt/hermes-helmet/SOURCE_COMMIT` matches the source commit you
   intended.

5. Resume the same job and check its state:

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
     /opt/hermes/.venv/bin/hermes cron resume github-issue-poller
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
     /opt/hermes/.venv/bin/hermes cron list
   ```

Keep the same named volume (`hermes_helmet_state` in the public Compose file,
or the company volume in a private overlay). Worker configuration, credentials,
checkouts, the poller ledger, and issue/task/PR associations live under
`/opt/data`. Replacing the image does not replace that volume.

Reconcile active work before changing `repositories[].slug` or
`repositories[].worktree`. Those mappings are how the poller finds checkouts
and existing assignments; moving them under a running intake can attach new
work to the wrong tree.

Publication of a new preview is a manual workflow on protected `main`. It tags
`ghcr.io/machinewisdomai/hermes-helmet/runtime:preview-<full-sha>` and does not
promote `latest` or `stable`. See [SECURITY.md](../SECURITY.md) for scan and
visibility notes.
