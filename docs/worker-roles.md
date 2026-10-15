# Worker roles

Hermes Helmet owns task routing, review reconciliation, idempotency, profile
provisioning, and capacity verification. Deployments supply company policy,
identity, paths, credentials, and the poll schedule. Adapters call this core
instead of copying its control loops.

Optional authority fields, available from 0.17.0; the consolidated writing role
and per-role reasoning policy are available from 0.18.0:

```json
{
  "assignee": "builder",
  "worker_roles": {
    "profiles": {
      "builder": "builder",
      "reviewer": "reviewer",
      "writing": "writing"
    },
    "max_in_progress": 12,
    "max_in_progress_per_profile": 4,
    "reasoning_effort": {
      "builder": "xhigh",
      "reviewer": "max",
      "writing": "high"
    }
  }
}
```

Merge these fields into a complete authority policy. All configured profiles must be
unique safe names; builder must equal `assignee`. Both limits must be positive
integers, with per-profile capacity no greater than global capacity. Omitting
`worker_roles` retains single-profile behavior. These are shared capacity caps,
not reserved slots or a fairness guarantee. Model lanes select inference
providers; worker roles select assignments and are independent of them.

Issues default to builder. A standalone `Worker role: writing` outside fenced
code selects writing, covering documentation, specifications, media, marketing,
and public writing. `Worker role: documentation` and `Worker role: media` remain
aliases for writing in a consolidated policy. Existing four-profile policies
with separate documentation and media roles remain valid and retain their routing.
Duplicate or invalid
declarations fail closed, including declarations without configured roles.
Native GitHub review requests select reviewer automatically. Unfinished review
attempts on the old builder still gate successor reviews. Repairs retain their
original task's assignment. Routing changes neither authority nor idempotency.
Existing queued tasks are not reassigned.

Optional `reasoning_effort` must specify `low`, `medium`, `high`, `xhigh`, or `max`
for every configured role. Provisioning writes each level as
`agent.reasoning_overrides[policy.inference_model]` after inheriting approved
builder settings, and verification checks it against policy. Reinstallation
therefore restores the role's accepted effort; other model overrides remain
intact. Omitting the field leaves effort unmanaged. The selected provider/model
must support the chosen levels.

## Installation and verification

The provisioner supports Hermes' bundled `ruamel.yaml` and PyYAML. Install the package's `worker` extra for PyYAML in environments without either parser.
Start with an approved builder profile, subprocess home, and company tools and
skills. Run as the worker UID. Pause Hermes dispatch and drain running tasks
on every board; the installer checks both before configuration writes.

`python -m hermes_helmet.install_poller --config POLICY --worker-state-root ROOT`
provisions configured roles. `--hermes`, `--gh`, and `--git-credential-helper`
select binaries and an optional exact Git helper expectation. Role provisioning
applies model and budget policy even with `--skip-model-config`.

Adapters that already own deployment transport can call the narrower module:

```sh
python -m hermes_helmet.worker_role_runtime --config POLICY --state-root ROOT --check-drained
python -m hermes_helmet.worker_role_runtime --config POLICY --state-root ROOT
python -m hermes_helmet.worker_role_runtime --config POLICY --state-root ROOT --verify
python -m hermes_helmet.worker_role_runtime --config POLICY --state-root ROOT --recover-identity PROFILE
```

Provisioning copies approved builder provider settings, `.env`, and Git config;
links company SOUL, skills, and executable tools; and leaves profile histories,
OAuth stores, memory, and cron separate. An unchanged builder persona is
recognized by exact installed stock content and file ownership, not by its
filename, length, or a marker. That persona is then linked to the accepted root
`SOUL.md` on the same path, so the company contract is delivered once and Hermes
operational guidance is left in the runtime prompt. The replaced persona is
copied under `.helmet-private/identity-backups/`, outside profile, skill, and
memory discovery, and can be restored while drained with `--recover-identity`.
Recovery stages and validates the replacement before swapping the active path, so
an occupied temporary path or a failed write leaves the prior identity untouched.
A custom persona or unexpected symlink stays byte-identical. Verification then
reports action required instead of success, and missing or stale company
delivery fails for every configured role. Each role must verify as the configured
worker GitHub identity. Failed provisioning restores modified leaves and removes
newly created profiles/links. Unrelated existing profile state is preserved.
Consolidating roles does not delete, copy, or combine old profile session stores.
Retain those profiles for historical exports and original-task repairs; migrate
only verified queued work through the deployment's supported assignment path.

The installer writes both global and profile limits, but never resumes dispatch
or restarts the gateway. A separately approved drained restart is required to
refresh cached scheduler limits. Verify effective capacity before resuming.
Cron consumes the policy `schedule` with `no_agent=true`; deployments may select
`every 1m`. Polling alone does not invoke an LLM.

Portable behavior tests belong here. Deployment tests cover actual adapter
entry points, policy projection, transport, receipts, and installation guards.
