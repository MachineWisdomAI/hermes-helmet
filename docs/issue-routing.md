# Issue routing

Track an issue in one repository while Hermes implements, reviews, and repairs
its code in another. The tracking issue stays the delivery and acceptance
identity. The code pull request lives in the implementation repository and does
not expose the tracking issue.

Typical use: an internal planning repository tracks the work, and a public
product repository receives the code. Example names below are generic.

## Configure a route

Routes are operator configuration in the authority policy
([authority-schema.md](authority-schema.md)). Issue text never creates or
widens one.

```json
"repositories": [
  {"slug": "example-org/planning", "worktree": "/opt/data/repos/planning"},
  {"slug": "example-org/product", "worktree": "/opt/data/repos/product"}
],
"issue_routes": [
  {
    "label": "route:product",
    "tracking_repository": "example-org/planning",
    "implementation_repository": "example-org/product"
  }
]
```

- Both repositories must already be in `repositories`; otherwise the policy is
  rejected. Routing adds no repository and no credential.
- A route applies only to issues in `tracking_repository` that carry `label`
  (together with the dispatch label). The same repository can still own its
  own documentation pull requests: an unlabeled planning issue is implemented in
  the planning checkout exactly as before.
- `label` must differ from `dispatch_label` and `ready_label`. Labels on one
  issue that select different targets are refused, not guessed.
- Omit `issue_routes` and behavior is unchanged.

Keep the route label on the tracking issue until it is accepted. Removing it
makes the live issue resolve to its own repository, and discovery then refuses
the foreign pull request instead of adopting it.

## What follows the route

| Stage | Behavior |
| --- | --- |
| Dispatch | The poller (or `helmet issue`) creates one root task keyed by the tracking issue URL, in the implementation checkout, with the implementation repository as the completion contract and an opaque canonical branch `automation/tracked-<hash>`. |
| Task instructions | The private task card names the tracking issue and carries a public pull-request privacy contract. |
| Discovery | The pull request is found in the implementation repository by the canonical branch (open or merged), or by exact-URL adoption from the ledger or task metadata. Timeline cross-references, issue numbers, links, and closing keywords never associate a routed pull request. |
| Target check | A foreign pull request is valid only for a configured target of the tracking repository, and the task's checkout must be that target's. Anything else fails closed. |
| Review and repair | The Captain reviews the exact head in the implementation repository. Trusted review events create the same-PR repair task in the target checkout, on the original branch, with the implementation repository's completion contract. |
| Merge | `helmet merge` merges the exact reviewed head (SHA-bound) in the implementation repository, reading its required checks. Merge-authority markers on the tracking issue still apply (`Merge when clean: no` holds the merge). |
| Acceptance | Merged code is not acceptance. The checkpoint moves to `AWAIT_ACCEPTANCE`, the tracking issue stays open, and `helmet accept` records acceptance explicitly. |

### Public pull-request privacy

The privacy contract tells the worker to keep the tracking issue's URL, number,
repository, title, body, labels, approval or merge-marker provenance, and any
closing keyword out of the pull request title and body, commits, branch name,
replies, and comments. Repair tasks repeat it. Helmet's own checks never need a
link back: association is by configured route plus the canonical branch.

This is an instruction plus structural safeguards (opaque branch, no
cross-reference lookups), not a content scanner. Review the public pull request
for leaks like any other change.

## Accept a routed issue

```sh
helmet accept TRACKING_ISSUE_URL --expected-head MERGED_HEAD_SHA --config policy.json
```

It runs as the Captain and acts only when the canonical pull request in the
configured target merged at `MERGED_HEAD_SHA` and that head received the clean
Captain review recorded in the checkpoint. It then comments on the tracking
issue with the pull request and short SHA, closes the tracking issue as
completed, re-reads it, and records `DONE`. A second call reports
`already_closed`. It never retries a failed close blindly. Same-repository
issues are refused: they close with their pull request as before.

Closing the tracking issue by hand has the same effect: status reports `DONE`
for a closed issue with a merged pull request, and the next `helmet issue` pass
records it.

Discovering that the pull request merged is not a Captain review. When a pass
finds a merged routed pull request, the checkpoint's `clean_head` and
`reviewed_head` stay as they were, so a merge made outside this workflow, or at
a head that was never reviewed clean, leaves `helmet accept` withholding with
`head_lacks_clean_captain_review`. A note records the unreviewed merged head.

A checkpoint in `AWAIT_ACCEPTANCE` is not readable by releases before this
state existed; upgrade the Captain host before routing issues.

### Dependencies and epics

Dependency state refers to the tracking issue. In an epic, a routed child whose
code merged is `awaiting_human` (not active, not complete), and a dependent
`Blocked by` that child stays blocked until the tracking issue is closed
completed, which `helmet accept` does. Native sub-issue and blocked-by
relationships remain on the tracking issue; no relationship is created on the
public pull request.

## Transferred tracking issues

GitHub gives a transferred issue a new URL. Without help, the poller would see a
new labeled issue and create a second root. Adopt the transfer explicitly:

```sh
helmet adopt-transfer OLD_ISSUE_URL NEW_ISSUE_URL --config policy.json \
  --ledger /path/to/ledger.sqlite3 --checkpoint-dir /path/to/checkpoints
```

Run it where the poller ledger lives. It validates, then changes, in this
order:

1. Both repositories are allowlisted, and the ledger has one root for the old
   URL and none for the new URL.
2. GitHub resolves the old URL to the new issue and both reads return the same
   `node_id`. Otherwise it refuses.
3. The root task (read only) is finished (`done`, `archived`) or held
   (`blocked`). Running, queued, ready, or triage tasks are refused.
4. The latest same-PR repair task named by the pull-request watch is read the
   same way: finished or held is accepted; running, queued, ready, triage, or
   an unreadable status is refused, because a finished root routinely has an
   active repair.
5. The task's checkout belongs to the new tracking repository itself or to a
   configured route target of it, and any recorded pull request is in that
   target.
6. The checkpoint move is proven possible. If a checkpoint already exists at the
   new URL, it must match the old one on start time, root task, state, pull
   request, review heads, pending repair, repair rounds, and merge choice.
   A conflicting or unreadable one is refused. Steps 1-6 write nothing.
7. The existing ledger rows (root, creation order, pull-request watch, review
   cursor, repair chain) are re-keyed to the new URL in one transaction, and the
   old URL is recorded as an alias so a retained link or the old canonical
   branch resolves to the same root.
8. The Captain checkpoint is carried to the new URL with `started_at`,
   `updated_at`, repair rounds, review heads, merge choice (including an
   explicit-approval hold), and state unchanged. The old checkpoint file is
   left in place.

Kanban is only read. Nothing is unblocked, restarted, reclaimed, replaced,
completed, or rewritten, so a blocked do-not-restart task stays blocked and
original budgets are not reset. Re-running is safe and finishes an interrupted
checkpoint move. After adoption, commands given the old URL act on the new one.

### Limits

- **Active tasks are not adopted.** A running or queued worker holds
  instructions that name the old tracking issue and, when the new tracking
  repository is private, may publish links to it. Safe adoption needs a
  separate runtime interface that can deliver amended instructions to, or
  quiesce, a live task. That interface does not exist here and this command does
  not provide it; wait for the task to finish or block, then adopt.
- **The ledger must be local.** If the Captain reaches the worker only through
  `HERMES_HELMET_WORKER_RUNTIME`, that adapter needs a verb that re-keys the
  ledger. Without it the command refuses (`transfer_ledger_unavailable`) and
  nothing changes.
- **Hold dispatch around the transfer.** Between the GitHub transfer and the
  adoption, the poller can see the transferred issue with its dispatch label and
  create a root. Remove the dispatch label before transferring, adopt, then
  restore it. Helmet cannot prove the issue is the same one before adoption.
- Existing task instructions are not rewritten, and an epic graph that lists
  the old URL needs the usual graph re-acceptance.

## Later live qualification

Fixtures prove the behavior above against an in-memory GitHub. These
assumptions need a live check before relying on them:

- A GitHub REST read of a retired issue URL follows the redirect and returns
  the new issue with the same `node_id` that `helmet adopt-transfer` compares.
- Whether the dispatch label survives a transfer, which decides how much of the
  dispatch hold is needed.
- Native sub-issue and blocked-by endpoints follow a transferred issue.
- A `head=owner:branch` pull-request listing returns merged pull requests of
  the canonical branch (`state=all`).
- Required checks and branch rules read from the implementation repository for
  a pull request authored by the worker.

## Not covered

No installation, worker restart or drain, production policy change, or real
issue migration is part of this capability. Routes do not enroll repositories
(named-request enrollment keeps its own rules) and do not move merge authority:
the Captain still reviews the exact head and the worker never merges.
