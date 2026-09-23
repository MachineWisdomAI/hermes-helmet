# Hermes Helmet

**Give your coding agent its own identity. Keep review and merge authority with you.**

Hermes Helmet turns GitHub issues into implementation, review, and repair work
for [Hermes Agent](https://github.com/NousResearch/hermes-agent). The worker uses
its own GitHub account, credentials, container, and checkout. A human-directed
Captain reviews its pull requests from a separate checkout and controls merging.
The worker does not receive the Captain's credentials.

```text
You / Captain                         Hermes worker
Your GitHub identity                  Its own GitHub identity
Scope work, review, authorize merge → Implement, test, open a PR
Review the PR                       → Repair the same PR
Merge when authorized                 Never merge
```

This is the first **Apache-2.0 public source preview**, published September 23,
2026. It includes the working control loop, setup, issue and epic orchestration,
and portable skills developed through real repository work. Read the
[preview notes and known limitations](docs/public-preview.md).

## Start here

You need Docker Compose, Python 3.11+, a separate GitHub account/token for your
worker, and credentials for your chosen model provider. Clone this repository
and install the CLI in a virtual environment:

```sh
git clone https://github.com/MachineWisdomAI/hermes-helmet-oss.git
cd hermes-helmet-oss
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
hermes-helmet --help
```

The ExampleCo fixture uses `example-captain` and `example-agent`; replace
those identities and its repository entries with your own.

Follow the [quickstart](docs/quickstart.md) to configure identities, repositories,
provider access, and the Docker runtime. The documented runtime builds from
source; no access to a private image registry is required.

OpenViking working memory, FAVA Trails governed decisions, and external skill
packs are optional. The core works without a private company repository or
private toolkit. Bundled Captain skills are maintained in `skills/` and packaged
from that single source.

## Why separate identities?

A pull request should show which agent wrote it and which person accepted it.
Giving the worker its own credentials and workspace makes that distinction
visible and lets you restrict its access independently. Repository policy limits
where Helmet dispatches work; GitHub permissions remain the actual access ceiling.

The Captain-and-Crew model combines a named human owner, bounded worker tasks,
visible GitHub reviews, same-PR repairs, and explicit merge policy. See the
[operating model](docs/captain-and-crew.md) and
[authority contract](docs/authority-schema.md).

The [Blueprint Alliance white paper](https://www.okta.com/content/dam/resources/en_us/whitepapers/Blueprint%20Alliance%20Whitepaper-Sep18-Final.pdf)
published during Oktane 2026 emphasizes distinct agent identities and traceable
delegation. Helmet applies that principle to a concrete coding workflow. It is
an independent project; this preview does not implement Okta Agent SSO or claim
Blueprint certification.

## Captain orchestration (helmet-issue / helmet-epic)

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli install-skills
PYTHONPATH=src python3 -m hermes_helmet.cli issue ISSUE_URL --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli status ISSUE_URL --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli epic EPIC_URL --config config/policy.json --accept-graph
PYTHONPATH=src python3 -m hermes_helmet.cli epic-status EPIC_URL --config config/policy.json
```

See [docs/helmet-issue.md](docs/helmet-issue.md),
[docs/helmet-epic.md](docs/helmet-epic.md), [docs/setup-helmet.md](docs/setup-helmet.md),
`skills/helmet-issue/SKILL.md`, `skills/helmet-epic/SKILL.md`, and
`skills/setup-helmet/SKILL.md`.

## Validate

```sh
scripts/verify.sh
```

## Development image

```sh
scripts/build-dev-image.sh
# optional publish:
# HERMES_HELMET_PUSH=1 scripts/build-dev-image.sh
```

Default base is the immutable official release
`nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294`
(Hermes Agent 0.21.3), shared by `deploy/Dockerfile`, Compose, and the build
script. The script writes `dist/image-identity.json` with the source commit and
image id/digest for wrapper consumers.

## Control loop

See [docs/control-loop.md](docs/control-loop.md).

## Authority contract

See [docs/authority-schema.md](docs/authority-schema.md) and
[docs/private-overlay.md](docs/private-overlay.md). The ExampleCo fixture lives
at `config/fixtures/exampleco/policy.json` and is mirrored by
`config/policy.example.json`.

## Optional FAVA Trails (governed company brain)

FAVA Trails is optional and outside the minimum runtime. Guided setup, doctor,
and an accepted-engine lifecycle demo (requires installed fava-trails + jj):

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli fava setup --config config/policy.json --write-templates
PYTHONPATH=src python3 -m hermes_helmet.cli fava lifecycle-demo --config config/policy.json
```

See [docs/fava-trails.md](docs/fava-trails.md). Canonical FAVA install and agent
contracts are linked there (not copied). OpenViking remains distinct optional
working context; promotion into FAVA is explicit.

## Optional OpenViking working context

OpenViking is optional AGPL-3.0 operational working context (not governed
truth). Helmet templates owner-only client configs and doctor/shared-proof
surfaces; Codex and Claude Code use the official OpenViking memory plugins
instead of a Helmet-owned MCP adapter. See
[docs/openviking.md](docs/openviking.md).

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json
PYTHONPATH=src python3 -m hermes_helmet.cli openviking setup --config config/policy.json
```

## Optional model lanes

Provider/model selection for the Hermes executor is independent of optional FAVA
generation, OpenViking semantic generation, and embedding lanes. Local
OpenAI-compatible endpoints are supported; declining them leaves the minimum
runtime up. See [docs/model-lanes.md](docs/model-lanes.md).

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli models setup --config config/policy.json
```

## Company skills

Optional executor skill packs import into Hermes-owned state only. Captain
skills install through an explicit setup action. See
[docs/company-skills.md](docs/company-skills.md).

## Public documents

- [Captain and Crew](docs/captain-and-crew.md)
- [Configuration reference](docs/authority-schema.md)
- [Private overlay seam](docs/private-overlay.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)
- [Public preview and limitations](docs/public-preview.md)
- [Container candidate tooling](docs/release-candidate.md)
- [Third-party notices](NOTICE)
- Generic agent guidance: [AGENTS.md](AGENTS.md)

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). OpenViking is an
optional AGPL-3.0 service boundary; enabling it is an adopter choice and is
not required to start Helmet. See [docs/openviking.md](docs/openviking.md).
