# FAVA Trails governed company brain

FAVA Trails is an **optional** Hermes Helmet integration. It is the governed
company brain and important-decisions ledger under **Apache-2.0**. Hermes Helmet
starts and operates when FAVA Trails is declined or unavailable.

OpenViking (when present) remains **operational working context**, not governed
truth. Promotion from OpenViking into FAVA Trails is an **explicit** decision
that leaves a governed trail — never automatic copying.

Helmet does **not** reimplement FAVA governance. Use the canonical contracts:

| Contract | Pin (H7 inspected baseline) |
| --- | --- |
| Installation | https://github.com/MachineWisdomAI/fava-trails/blob/10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7/README.md |
| Agent setup | https://github.com/MachineWisdomAI/fava-trails/blob/10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7/AGENTS_SETUP_INSTRUCTIONS.md |
| Governed recall | https://github.com/MachineWisdomAI/fava-trails/blob/10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7/docs/governed-recall.md |

Inspected baseline: `10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7` (0.7.0, MCP 2.2).

## What this integration provides

- Guided setup templates for a company-owned FAVA data repository (no secrets).
- Ordinary Captain and executor principals with distinct `FAVA_TRAILS_AGENT_ID`
  values and one shared company scope.
- Owner-only principal config files (mode `0600`) that never embed API keys.
- `hermes-helmet doctor` offline checks (and optional live `fava-trails doctor`).
- A hermetic governed lifecycle demo on the **accepted FAVA engine**: draft
  isolation, proposal, Trust Gate approve/reject, frozen approved content,
  supersession/provenance, approved shared recall, and explicit OpenViking→FAVA
  promotion (TrustResult injected; no network / no private credentials).


## Complementary responsibilities

| System | Role |
| --- | --- |
| OpenViking | Operational working context across clients |
| FAVA Trails | Governed decisions, observations, validation, lineage |

## Identity boundary (ordinary principals)

- Authoring is bound to a **dedicated process identity** via
  `FAVA_TRAILS_AGENT_ID`. A scope hint alone is **not** an authorization boundary.
- Captain and executor are **ordinary** principals. Never set
  `FAVA_TRAILS_OPERATOR=1` on those endpoints.
- Operator/history/human-approval tools stay on a separate operator-controlled
  process. Do not give the executor raw data-repo or operator access that defeats
  the ordinary-principal boundary.
- Tool arguments cannot establish a principal or grant operator powers.

## Setup (guided, optional)

```sh
# Non-secret company template + principal/MCP examples
PYTHONPATH=src python3 -m hermes_helmet.cli fava setup \
  --config config/policy.json --write-templates \
  --fava-root config/fixtures/exampleco/fava-trails \
  --company-scope exampleco/engineering

# Optional empty-directory scaffold (config.yaml + trust-gate prompt only).
# Does NOT run fava-trails bootstrap over existing data.
PYTHONPATH=src python3 -m hermes_helmet.cli fava setup \
  --config config/policy.json \
  --scaffold-data-repo /tmp/example-fava-data
```

Follow canonical FAVA install to bootstrap or **clone** the company data repo.
Do not bootstrap over an existing remote data repository.

Provision `OPENROUTER_API_KEY` (or the configured env name) out of band. Write
owner-only principal configs with a real data-repo path:

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli fava write-principal \
  --config config/policy.json \
  --fava-root ~/.hermes-helmet/fava-trails \
  --principal captain \
  --data-repo /opt/data/company-fava-trails-data \
  --output ~/.hermes-helmet/fava-trails/principals/captain/principal.json

PYTHONPATH=src python3 -m hermes_helmet.cli fava write-principal \
  --config config/policy.json \
  --fava-root ~/.hermes-helmet/fava-trails \
  --principal executor \
  --data-repo /opt/data/company-fava-trails-data \
  --output ~/.hermes-helmet/fava-trails/principals/executor/principal.json
```

Register **separate** MCP server processes per principal using the emitted
`*.mcp.json.example` shapes. Substitute secrets from owner-only stores only.

## Doctor

```sh
# Always safe: declined integration skips OK
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json

# When integrations.fava_trails is true:
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json \
  --fava-root ~/.hermes-helmet/fava-trails

# Optional live probe when fava-trails CLI is installed
PYTHONPATH=src python3 -m hermes_helmet.cli doctor --config config/policy.json \
  --fava-root ~/.hermes-helmet/fava-trails --live
```

Doctor verifies configuration, intended company scopes, ordinary principal
boundaries, and data-repo structure **without echoing secrets**. Offline mode
does not authenticate Trust Gate keys. Live mode binds diagnostics to the
**selected** data root and principal configs, verifies process-owned identity
and MCP authorization boundaries through accepted FAVA interfaces, and fails
closed on protocol/domain errors (it does not merely inherit ambient
`FAVA_TRAILS_DATA_REPO` or a bare CLI exit code).


## Verified lifecycle example

```sh
PYTHONPATH=src python3 -m hermes_helmet.cli fava lifecycle-demo \
  --config config/policy.json --json
```

This example uses the **accepted FAVA TrailManager** (JJ-backed) with injected
TrustResult verdicts so protocol behavior is reproducible without network or
private credentials:

1. Draft isolation (governed recall cannot see drafts)
2. Proposal by the captain ordinary principal
3. Trust Gate rejection fail-closed (approved truth unchanged / still hidden)
4. Trust Gate approval → frozen attributable content
5. Executor governed shared recall of approved records
6. Supersession/correction with lineage (`supersedes_id` / `superseded_by`)
7. Explicit OpenViking→FAVA promotion metadata (`automatic: false`)

Boundaries this example does **not** claim:

- Live Captain/executor MCP tool discovery in a foreground app
- Private adopter acceptance or production Trust Gate LLM calls
- Automatic OpenViking memory ingestion

## Minimum runtime boundary

The public Compose stack does **not** run FAVA Trails. Private overlays may add
tenancy. See [private-overlay.md](private-overlay.md) and
[authority-schema.md](authority-schema.md).

Enable the integration only by setting `integrations.fava_trails` to `true` in
the adopter authority policy after principals and the company data repo are
ready.
