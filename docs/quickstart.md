# Hermes Helmet quickstart

Start with one bounded GitHub issue. This guide sets up the Hermes worker,
links the issue to its Kanban assignment, and introduces the Captain commands
for following the pull request through review and acceptance.

The worker runs in Docker. The Captain runs in your coding-agent host with a
separate GitHub identity. FAVA Trails, OpenViking, private company repositories,
and external skill packs are optional. The bundled `setup-helmet`,
`helmet-issue`, and `helmet-epic` skills provide the Captain workflow.

## Prerequisites

- Docker and Docker Compose v2
- A GitHub token for the worker identity named in your policy
  (`worker_github_login` / H1 `github_identity`)
- Python 3.11+ for the host CLI and tests
- Model-provider access for the Hermes worker
- For Captain orchestration: a separate Captain GitHub identity and the
  portable `helmet-issue` / `helmet-epic` skills. The preview has been exercised
  through Codex; Claude Code and Hermes have installation and static validation.

The published worker image is a separate installation from this CLI. Installing
`helmet` does not pull GHCR or start Docker. The current preview digest is
`ghcr.io/machinewisdomai/hermes-helmet/runtime@sha256:f6e2375441cec50cac370941f4bfa53e7b2af0b8bff45ee177da6e49b760caea`,
built from `c0fc5d883b56befcf1bd8354c6dab7c612ac6455`. See
[runtime-image.md](runtime-image.md). Development builds still pin the official
Hermes Agent base
`nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294`
(Hermes Agent 0.21.3). Override only with another immutable tag+digest; do not
use `latest` or `main`.

## Install the CLI

Clone and install the current public source:

```sh
git clone https://github.com/MachineWisdomAI/hermes-helmet.git
cd hermes-helmet
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
helmet --help
```

The help output lists the Captain commands. Current source installs `helmet`
and its compatible alias `hermes-helmet`; the Python package is named
`hermes-helmet`. If you installed the September 23 preview wheel, use
`hermes-helmet` in place of `helmet` in these examples.

## 1. Configure

For a new adopter, start from the secret-free setup questionnaire:

```sh
cp config/setup.answers.example.json /private/path/setup.answers.json
# replace every ExampleCo identity, repository, checkout, and provider value
helmet setup --answers /private/path/setup.answers.json --json
helmet doctor --config ~/.hermes-helmet/policy.json --home ~ --live --json
```

Setup-state doctor is a pre-dispatch gate. With `--home` it performs the live
worker identity, configured work/action allowlist, label, and provider/model checks even if
`--live` is omitted; failures cannot be reported as skipped-and-green. The
flag remains in this command to make the operator's intent explicit. The
legacy doctor path without setup state keeps its offline default.

Setup prompts locally without echo for the worker PAT and provider credential.
It writes both only under `~/.hermes-helmet/secrets` with owner-only modes. Keep
the answers file secret-free. OpenViking and FAVA Trails remain disabled until
their `selected` and `confirmed` values are both true. Matt Pocock skills and
gstack remain pinned recommendations and are never installed by setup.

The default live model probe supports OpenAI. Another provider must expose an
OpenAI-compatible endpoint configured as
`model_lanes.hermes_executor.base_url`; unsupported providers fail before setup
creates labels or writes state. Hermes Helmet state and secrets directories must be
owned by the current user with mode `0700`, credential files must be regular
owner-owned `0600` files, and symlinks are rejected. Pre-dispatch doctor checks
every existing component of the supplied home, Hermes Helmet state/generated/secret
paths, and Codex/Claude/Hermes skill roots and destinations is
current-user-owned, non-symlink, and not group/other writable. It also checks every configured origin fetch and
push URL against the repository slug, admitting only credential-free canonical
GitHub HTTPS and supported GitHub SSH forms.

Before prompting for credentials, probing providers, creating labels, or writing
state, setup preflights every bundled-skill destination. Any foreign skill
conflict stops setup without changing local or GitHub state.
If an unexpected skill apply failure occurs after preflight, the atomic skill
transaction rolls back new destinations and setup records an incomplete,
resumable skills stage. A destination added after preflight is reported as a
conflict and never promotes state to complete. After removing the conflict,
rerunning the same setup reuses the PAT and any labels
already created, completes skill installation, and marks the state complete.

The manual minimum-runtime path remains available:

```sh
cp deploy/.env.example deploy/.env
# set HERMES_GITHUB_TOKEN=
# uncomment HERMES_HELMET_IMAGE and keep the published digest

cp config/policy.example.json config/policy.json
# edit company, captain_github_login, worker_github_login, assignee,
# repositories[].slug, repositories[].worktree
```

The example policy is the generic ExampleCo authority fixture
(`example-org/demo-repo`, `example-captain`, `example-agent`, `builder`).
Replace those values before enabling intake. See
[authority-schema.md](authority-schema.md) and
[private-overlay.md](private-overlay.md).

`render_crew_contract(policy)` prints the crew contract from that document. It
does not install instructions. After you create the assignee profile, copy the
reviewed contract into that profile's `SOUL.md` at
`$HERMES_HOME/profiles/<assignee>/SOUL.md`. ExampleCo uses profile `builder`
and Compose `HERMES_HOME=/opt/data`.

```sh
PYTHONPATH=src python3 - <<'PY'
from pathlib import Path
from hermes_helmet.authority import load_authority, render_crew_contract
policy = load_authority(Path("config/policy.json"))
print(render_crew_contract(policy))
PY
```

## 2. Start

From the repository root, pull the published digest and start Compose without
building. Put that digest in `deploy/.env` as `HERMES_HELMET_IMAGE`. Compose
interpolates `image:` from that file. A leftover shell export of
`HERMES_HELMET_IMAGE` overrides `.env`; `docker pull "$HERMES_HELMET_IMAGE"`
does not read `.env`. If this shell previously exported the variable, unset it
so `.env` wins, then pull and start through Compose:

```sh
unset HERMES_HELMET_IMAGE
docker compose --env-file deploy/.env -f deploy/compose.yaml pull hermes
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --no-build
```

The image was built from `c0fc5d883b56befcf1bd8354c6dab7c612ac6455`. Confirm
the running container recorded that commit:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  cat /opt/hermes-helmet/SOURCE_COMMIT
```

The digest identifies the image built from that recorded source SHA.
`SOURCE_COMMIT` inside the container is that SHA. The `0.1.0rc1` package label
on the image is not a new stable release and is not the September 23 wheel.

The image entrypoint writes the worker token to its owner-only runtime file,
copies the mounted policy into `/opt/data/github-issue-poller/policy.json`,
removes the raw token variables, and only then enters the upstream s6 init
chain. Supervised dashboard and gateway processes therefore do not inherit the
PAT, while `/usr/local/bin/gh` can read it for one authorized GitHub command.

To wrap `deploy/Dockerfile` around a local checkout instead, select
`hermes-helmet:local` explicitly. Unsetting the shell variable is not enough
when `deploy/.env` still pins the published digest:

```sh
export HERMES_HELMET_SOURCE_COMMIT="$(git rev-parse HEAD)"
export HERMES_HELMET_IMAGE=hermes-helmet:local
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
```

That development `SOURCE_COMMIT` should match `git rev-parse HEAD`. Keep
company-specific Compose, policy, and volumes in a
[private overlay](private-overlay.md).

## 3. Clone the allowlisted checkout in the worker volume

The poller installer requires every policy `repositories[].worktree` to exist
as a Git checkout the worker can see. Compose persists that tree on the
`hermes_state` volume under `/opt/data`. The ExampleCo fixture uses slug
`example-org/demo-repo` and worktree `/opt/data/repos/demo-repo`. Replace those
placeholders with your allowlisted repository before enabling intake.

Do this in the worker container, with the worker GitHub identity. Do not clone
into a Captain checkout or reuse Captain credentials.

Set the wrapper-backed GitHub credential helper globally before cloning so
HTTPS clone can authenticate. After the checkout exists, set worker identity
and the same helper in that repository's local Git config. Linked worktrees
inherit those local values, including Kanban checkouts that use a different
home than Compose `HOME`. Do not run `gh auth setup-git`; it can record the
underlying `gh` binary and bypass `/usr/local/bin/gh`.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  mkdir -p /opt/data/repos
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git config --global --replace-all credential.https://github.com.helper \
  '!gh auth git-credential'
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git clone https://github.com/example-org/demo-repo.git \
  /opt/data/repos/demo-repo
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git -C /opt/data/repos/demo-repo config --local user.name "example-agent"
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git -C /opt/data/repos/demo-repo config --local user.email \
  "example-agent@users.noreply.github.com"
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git -C /opt/data/repos/demo-repo config --local --replace-all \
  credential.https://github.com.helper '!gh auth git-credential'
```

The helper asks `/usr/local/bin/gh` for GitHub HTTPS credentials. That wrapper
reads the owner-only runtime token file. Do not put a token in the clone
command, in Git config, in policy, or in source files. The worker Git identity
is your `worker_github_login` (`example-agent` in the fixture), never
`example-captain`.

Confirm the path the installer will check:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  git -C /opt/data/repos/demo-repo rev-parse --is-inside-work-tree
```

## 4. Create the worker profile and complete provider login

Kanban work runs as the policy `assignee` Hermes profile (`builder` in the
ExampleCo fixture). Create that profile in the worker container, set the
policy provider and model, then complete that profile's own provider login.
Keep Captain and worker identities distinct: do not copy Captain host
credentials, sessions, or profiles into the worker.

These `hermes` invocations are the in-container Hermes Agent CLI. They are not
the host Hermes Helmet command. Current source installs that host CLI as
`helmet`, with compatible alias `hermes-helmet`; the dated preview wheel
exposes only `hermes-helmet`.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes profile create builder
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set model.provider openai
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set model.default gpt-4.1
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder config set agent.max_turns 100
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder auth add openai
```

`auth add` prompts locally for the credential. Do not pass `--api-key` or
put provider secrets in commands or files. Match `model.provider` /
`auth add` to your policy `inference_provider` and `inference_model`. Then
make one bounded smoke call:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/hermes -p builder chat --oneshot --max-turns 1 \
  -q "Reply with the single word ready."
```

OpenViking, FAVA Trails, Signal, and company skill packs stay optional and
remain disabled in the ExampleCo fixture. Do not enable them on this path.

## 5. Install the poller cron (once)

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/python -m hermes_helmet.install_poller \
  --config /opt/data/github-issue-poller/policy.json \
  --skip-model-config
```

Use `--skip-model-config` so install keeps the assignee profile you configured
and smoke-tested in the previous step instead of rewriting provider, model, and
max turns. The installer does not perform provider login, and this flag does
not prove credentials work; the bounded smoke call does. The installer still
validates allowlisted worktrees and reconciles exactly one no-agent cron job
(`cron_deliver` defaults to `local`).

## 6. Trigger once

Label an allowlisted open issue with the policy `dispatch_label` (default
`hermes-kanban-go`), then:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/python -m hermes_helmet \
  --config /opt/data/github-issue-poller/policy.json
```

An eligible issue should now have one linked Kanban root task. Repeating intake
for that issue reuses the existing task. Follow the worker's linked pull request
when implementation finishes; task completion alone does not accept or merge it.

## 7. Captain-side helmet-issue / helmet-epic (optional host)

On the Captain workstation (not the worker container identity):

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli install-skills --target codex
PYTHONPATH=src python3 -m hermes_helmet.cli issue \
  https://github.com/example-org/demo-repo/issues/123 \
  --config config/policy.json \
  --host-continuation none --one-pass-only
PYTHONPATH=src python3 -m hermes_helmet.cli status \
  https://github.com/example-org/demo-repo/issues/123 \
  --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli wait \
  https://github.com/example-org/demo-repo/issues/123 \
  --timeout-seconds 1800
PYTHONPATH=src python3 -m hermes_helmet.cli epic \
  https://github.com/example-org/demo-repo/issues/42 \
  --config config/policy.json \
  --accept-graph --host-continuation none --one-pass-only
PYTHONPATH=src python3 -m hermes_helmet.cli epic-status \
  https://github.com/example-org/demo-repo/issues/42 \
  --config config/policy.json
```

`wait` holds one bounded, read-only worker-side process. Exit `0` means run one
fresh issue or epic pass. Exit `2` is a clean timeout. Pass the returned cursor
to the next wait after every outcome. Exit `1` is a transport or contract
failure.

See [helmet-issue.md](helmet-issue.md) and [helmet-epic.md](helmet-epic.md).

## What this runtime does

- Creates one Kanban root task per eligible issue (idempotent by issue URL)
- Ignores closed, unlabeled, malformed, and non-allowlisted issues
- After a PR exists, trusted review activity creates one dependent same-PR repair
- Coalesces activity behind an outstanding repair; later activity can succeed it
- Never merges from the worker and never force-pushes
- Verifies the live GitHub login is the configured worker (Captain/unknown/mismatch fail closed)
- Captain `helmet-issue` adopts work, reviews heads, and applies merge gates without a second poller
- Captain `helmet-epic` validates parent/child graphs and invokes helmet-issue with bounded parallelism

Optional services and external skill import are intentionally out of this quickstart.
See [fava-trails.md](fava-trails.md) when enabling the optional governed company
brain after the minimum runtime is healthy. OpenViking working context is
optional and documented in [openviking.md](openviking.md). When you add a
company pack later, see [company-skills.md](company-skills.md). Optional local
or hosted model lanes are documented in [model-lanes.md](model-lanes.md); omit
them for the minimum runtime.
