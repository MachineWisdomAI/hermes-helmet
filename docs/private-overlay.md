# Private overlay seam

Hermes Helmet owns reusable authority schema, validation, renderers, poller
identity checks, and portable preflight helpers. Private products such as
WisdomHelm own concrete company values and deployment wiring.

## What the public core accepts

A private overlay is a version-2 authority JSON document plus runtime mounts:

| Overlay input | Consumed by | Notes |
| --- | --- | --- |
| `policy.json` (version 2) | poller, install helper, preflight, renderers | Single source of truth |
| Optional rendered `SOUL.md` / crew contract | Hermes profile seed | May be generated with `render_crew_contract` |
| Secrets outside the policy | container env / owner-only files | Never copied into policy or task prose |
| Compose service extras | private wrapper only | Signal, WisdomLoop, private FAVA/OpenViking tenancy |
| Optional FAVA principal configs | owner-only files outside policy | See [fava-trails.md](fava-trails.md); never embed keys in policy |
| OpenViking client configs | owner-only files per client | Codex/ChatGPT/Hermes each get a separate mode-0600 config; see [openviking.md](openviking.md) |
| Optional model-lane runtime configs | owner-only files / env | Provider secrets never enter policy; see [model-lanes.md](model-lanes.md) |

## What stays private

- Captain and worker GitHub logins for the real company
- Real repository slugs and checkout paths
- PATs, API keys, provider accounts, host restrictions
- Private service policy (Signal targets, Langfuse, etc.)
- Private skill allowlists and runbooks
- Concrete `skills.company_pack.source` paths and allowlists for private packs

## Integration pattern

1. Depend on an immutable Hermes Helmet image digest.
2. Mount or install your private `policy.json` at the runtime path expected by
   the poller (default `/opt/data/github-issue-poller/policy.json`).
3. Call public helpers instead of forking them:

```python
from hermes_helmet.authority import (
    load_authority,
    render_crew_contract,
    verify_worker_identity,
)
from hermes_helmet.preflight import StaticIdentityProbe, assert_worker_ready

policy = load_authority(path_to_private_policy)
contract = render_crew_contract(policy)
assert_worker_ready(policy, StaticIdentityProbe(observed_login))
```

4. Keep concrete adopter names (`example-captain` replaced by the real Captain
   login, `example-agent` replaced by the real worker login, private repos,
   hosts, service labels) only in the private overlay and private tests.

## Compatibility guarantee

- H1 version-1 documents remain loadable by the poller.
- H2 version-2 documents are a strict extension of that same policy family.
- Public fixtures and defaults stay generic (`ExampleCo`, `example-org`,
  `example-captain`, `example-agent`).

No submodule of Hermes Helmet into the private repository is required. Overlay
values flow in through configuration and mounts, not source forks.
