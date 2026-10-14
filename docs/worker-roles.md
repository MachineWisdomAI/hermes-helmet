# Worker roles

Hermes Helmet owns task routing, review reconciliation, idempotency, profile
provisioning, and capacity verification. Deployments supply company policy,
identity, paths, credentials, and the poll schedule. Adapters call this core
instead of copying its control loops.

Optional authority fields, available from 0.17.0:

```json
{
  "assignee": "builder",
  "worker_roles": {
    "profiles": {
      "builder": "builder",
      "reviewer": "reviewer",
      "documentation": "documentation",
      "media": "media"
    },
    "max_in_progress": 4,
    "max_in_progress_per_profile": 2
  }
}
```

Merge these fields into a complete authority policy. All four profiles must be
unique safe names; builder must equal `assignee`. Both limits must be positive
integers, with per-profile capacity no greater than global capacity. Omitting
`worker_roles` retains single-profile behavior. These are shared capacity caps,
not reserved slots or a fairness guarantee. Model lanes select inference
providers; worker roles select assignments and are independent of them.

Issues default to builder. A standalone `Worker role: documentation` or
`Worker role: media` outside fenced code selects that role. Duplicate or invalid
declarations fail closed, including declarations without configured roles.
Native GitHub review requests select reviewer automatically. Unfinished review
attempts on the old builder still gate successor reviews. Repairs retain their
original task's assignment. Routing changes neither authority nor idempotency.
Existing queued tasks are not reassigned.

## Installation and verification

Install the package's `worker` extra for PyYAML in non-Hermes environments.
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
```

Provisioning copies approved builder provider settings, `.env`, and Git config;
links company SOUL, skills, and executable tools; and leaves profile histories,
OAuth stores, memory, and cron separate. Each role must verify as the configured
worker GitHub identity. Failed provisioning restores modified leaves and removes
newly created profiles/links. Unrelated existing profile state is preserved.

The installer writes both global and profile limits, but never resumes dispatch
or restarts the gateway. A separately approved drained restart is required to
refresh cached scheduler limits. Verify effective capacity before resuming.
Cron consumes the policy `schedule` with `no_agent=true`; deployments may select
`every 1m`. Polling alone does not invoke an LLM.

Portable behavior tests belong here. Deployment tests cover actual adapter
entry points, policy projection, transport, receipts, and installation guards.
