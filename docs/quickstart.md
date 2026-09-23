# Hermes Helmet quickstart

This path starts the minimum control-loop runtime. It does **not** require
WisdomHelm, WisdomLoop, `wise-agents-toolkit`, FAVA Trails, OpenViking, Signal,
or any external skill pack. Bundled Captain skills (`skills/setup-helmet`,
`skills/helmet-issue`, `skills/helmet-epic`) are optional on the Captain host.

## Prerequisites

- Docker and Docker Compose v2
- A GitHub token for the worker identity named in your policy
  (`worker_github_login` / H1 `github_identity`)
- Python 3.11+ only if you run the hermetic tests on the host
- For Captain orchestration: a separate Captain GitHub identity and the
  portable `helmet-issue` / `helmet-epic` skills (Codex dogfood; Claude/Hermes
  install+static)

The development image defaults pin the official Hermes Agent base
`nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294`
(Hermes Agent 0.21.3). Override only with another immutable tag+digest; do not
use `latest` or `main`.

## Install the CLI

From a clone of the public repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## 1. Configure

For a new adopter, start from the secret-free setup questionnaire:

```sh
cp config/setup.answers.example.json /private/path/setup.answers.json
# replace every ExampleCo identity, repository, checkout, and provider value
hermes-helmet setup --answers /private/path/setup.answers.json --json
hermes-helmet doctor --config ~/.hermes-helmet/policy.json --home ~ --live --json
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
creates labels or writes state. Helmet state and secrets directories must be
owned by the current user with mode `0700`, credential files must be regular
owner-owned `0600` files, and symlinks are rejected. Pre-dispatch doctor checks
every existing component of the supplied home, Helmet state/generated/secret
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

cp config/policy.example.json config/policy.json
# edit company, captain_github_login, worker_github_login, assignee,
# repositories[].slug, repositories[].worktree
```

The example policy is the generic ExampleCo authority fixture
(`example-org/demo-repo`, `example-captain`, `example-agent`, `builder`).
Replace those values before enabling intake. See
[authority-schema.md](authority-schema.md) and
[private-overlay.md](private-overlay.md).

Render a crew contract from the same document when seeding a profile:

```sh
PYTHONPATH=src python3 - <<'PY'
from pathlib import Path
from hermes_helmet.authority import load_authority, render_crew_contract
policy = load_authority(Path("config/policy.json"))
print(render_crew_contract(policy))
PY
```

## 2. Start

From the repository root:

```sh
export HERMES_HELMET_SOURCE_COMMIT="$(git rev-parse HEAD)"
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
```

Confirm the image recorded its source commit:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  cat /opt/hermes-helmet/SOURCE_COMMIT
```

The image entrypoint writes the worker token to its owner-only runtime file,
removes the raw token variables, and only then enters the upstream s6 init
chain. Supervised dashboard and gateway processes therefore do not inherit the
PAT, while `/usr/local/bin/gh` can read it for one authorized GitHub command.

## 3. Install the poller cron (once)

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/python -m hermes_helmet.install_poller \
  --config /opt/data/github-issue-poller/policy.json \
  --skip-model-config
```

Use `--skip-model-config` until the worker profile has a real provider login.
The installer still validates allowlisted worktrees and reconciles exactly one
no-agent cron job (`cron_deliver` defaults to `local`).

## 4. Trigger once

Label an allowlisted open issue with the policy `dispatch_label` (default
`hermes-kanban-go`), then:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec hermes \
  /opt/hermes/.venv/bin/python -m hermes_helmet \
  --config /opt/data/github-issue-poller/policy.json
```

## 5. Captain-side helmet-issue / helmet-epic (optional host)

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
