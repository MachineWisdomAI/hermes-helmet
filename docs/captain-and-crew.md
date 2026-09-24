# Captain and Crew

Hermes Helmet is an open-source software factory that keeps delegated work
connected through implementation, review, repair, and acceptance. You set the
outcome and policy. The Captain coordinates and reviews on your behalf, while
a separate Hermes worker implements and repairs changes in allowed repositories.

The Captain runs through bundled skills in a coding-agent host, such as Codex.
The host uses the Captain GitHub identity and a checkout separate from the
worker's. The human owner determines what may be delegated and merged. These
roles share one authority policy; the worker receives only its own credentials.

## Roles

| Role | Identity | Authority |
| --- | --- | --- |
| Captain | `captain_github_login` (ExampleCo: `example-captain`) | Review, merge, epic dispatch, setup |
| Executor / worker | `worker_github_login` (ExampleCo: `example-agent`) | Bounded Kanban work, pull requests, never merge |
| Trusted humans | GitHub repository roles `OWNER`, `MEMBER`, `COLLABORATOR` | Repair-triggering review |
| Trusted bots | Exact logins in `trusted_review_bots` | Repair-triggering review |

Captain and worker must be different GitHub accounts. Preflight and the poller
fail closed when the live login is missing, is the Captain, or does not match
the configured executor. The Captain-side `helmet-issue` path fails closed if it
is running as the worker.

The worker never falls back to the Captain's credentials, sessions, checkouts,
browser state, or memory.

## One authority document

Company name, identities, labels, repositories, provider/model, merge policy,
budgets, and optional integrations come from one version-2 JSON policy. See
[authority-schema.md](authority-schema.md). The public ExampleCo fixture is
`config/fixtures/exampleco/policy.json` (copied as `config/policy.example.json`).

That document drives:

- the rendered crew contract (`render_crew_contract`)
- worker preflight (`assert_worker_ready`)
- poller identity checks and generated task language
- doctor/setup diagnostics

Secrets never belong in the policy. PATs and provider keys are runtime-only
owner-only files. Validation errors name the invalid field and do not print
secret values.

## Control loop

1. Label an allowlisted open issue with the policy `dispatch_label`.
2. The poller creates one Kanban root task for that issue URL.
3. The executor opens a tested pull request as the configured worker and
   records `metadata.published_pr`.
4. Trusted review on that pull request can create one dependent same-PR repair.
5. Captain `helmet-issue` reviews each head and applies the merge gate.
6. Captain `helmet-epic` runs independent children with bounded parallelism.
   The epic root is never dispatch-labeled.

The worker never merges and never force-pushes. Merge defaults to explicit
Captain approval. Silence is not approval. An unambiguous `Merge when clean: yes`
directive on an issue or epic root grants unattended merge only after current-head
review, required checks, and mergeability pass. Private-plan `local-only` worker
completion does not treat GitHub `mergeable_state=clean` as those required checks.

GitHub remains authoritative for issues, pull requests, checks, reviews, heads,
and merge state. Hermes Helmet does not copy review prose into Kanban and does not
start repairs from check failure alone.

## Delivery contract

The issue's accepted outcome defines completion. Reviews identify consequential
failures and missing required acceptance, with related findings grouped in one
pass. Small wording or formatting improvements are nonblocking. Repair reviews
check the changed behavior and affected boundaries; they reuse valid evidence
and still verify the current head. A clean PR needs no invented repair.

Real worker access and runtime assumptions are exercised before dependent
implementation or evidence tooling. Plans use actual capacity and keep optional
features outside the release critical path. Code readiness, runtime acceptance,
historical process compliance, and publication authority are separate results.

When repeated repairs reveal a wrong approach, an authorized Captain stops the
old attempt, preserves its useful work, and supersedes it with a distinct issue
and smaller implementation skeleton. Existing task identity, merge authority,
and epic graph checks still apply. Cancellation is controller-specific; the
portable CLI does not offer a cancellation verb. If execution cannot be safely
stopped, the successor remains undispatched.

These decisions are described in the [bundled review and recovery guide](../skills/helmet-issue/references/delivery.md).
It installs with the Captain skill and is available without this source
checkout or any external review, planning, or company skill pack. Optional
skills can help apply the contract; their installation is not an acceptance gate.

One `helmet wait` process and cursor per active child owns unchanged
waiting. The Captain resumes for meaningful changes or a bounded timeout,
reconciles once, and continues pending work. Repeated model turns announcing an
unchanged waiter waste quota; an extra watcher does not improve continuity.

## Six-question closeout

Every meaningful run must leave evidence that answers:

1. What did it read?
2. What did it write or change?
3. Which tools, accounts, and external systems did it touch?
4. What recommendations or decisions did it produce?
5. Did it stay inside the allowlist?
6. Where did the human approve, reject, or correct it?

## Optional integrations

OpenViking (AGPL-3.0 working context), FAVA Trails (governed decisions), Signal,
company skill packs, and extra model lanes are optional. Declining them leaves
the minimum runtime up. Doctor reports skipped integrations as skipped, not as
failures. Private overlays supply concrete company values without forking this
repository; see [private-overlay.md](private-overlay.md).
