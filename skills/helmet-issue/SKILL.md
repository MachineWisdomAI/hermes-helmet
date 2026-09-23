---
name: helmet-issue
description: "Orchestrate one GitHub issue through Hermes Helmet: preflight, dispatch or adopt, wait, review published heads, repair through the poller, and apply the merge gate."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
metadata:
  hermes:
    tags: [hermes-helmet, captain, orchestration, github, review]
    related_skills: []
---

# helmet-issue

## Overview

`helmet-issue` is the portable Captain-side orchestration skill for **one**
GitHub issue. Given `ISSUE_URL`, one invocation owns the loop until a truthful
terminal state. It adopts already-running work, discovers the worker pull
request without a human nudge, reviews each current head from a separate
Captain checkout, posts formal GitHub findings, and lets the existing H1 poller
create same-PR repair work.

It must **not** create a second watcher, webhook, repair relay, queue, or
worker checkout. GitHub remains authoritative for issues, PRs, checks, reviews,
heads, and merge state.

## When to Use

- Operator says `helmet-issue ISSUE_URL` or asks to Captain-orchestrate one issue
- An already-open worker PR must be discovered and reviewed without a user nudge
- Reinvocation after interruption (resume from checkpoint + live GitHub)

Do not use for:

- Epic/multi-issue trains → `helmet-epic`
- Worker execution inside a Kanban task (worker never merges, never runs this skill as worker)
- Inventing defects on a clean PR

## State model

```
PREFLIGHT → DISPATCH → WAIT_PR → REVIEW_HEAD → REQUEST_REPAIR → WAIT_REPAIR
        → REVIEW_HEAD … → READY → MERGE_GATE → optional MERGE → VERIFY_MERGED → DONE
```

Terminal stop states: `DONE`, `BLOCKED`, `FAILED`.

## Hard rules

1. Active GitHub identity MUST be the configured **Captain** and MUST differ from
   the configured **worker**. Fail closed on mismatch.
2. Never write the worker worktree or worker branch. Captain review checkout is
   separate and read-only with respect to the worker branch tip.
3. Never create Kanban repair tasks. Post formal GitHub review; H1 poller reacts.
4. Do not create repair from: approval-only reviews, untrusted activity, worker
   self-activity, repeated polling, or CI failure without a verified finding.
5. A changed PR head invalidates any prior clean result; re-review the new head.
6. Default merge mode stops for explicit Captain approval. Only an unambiguous
   whole-line `Merge when clean: yes` on the issue (or a live parent epic that
   still lists this child via **live native parent** association preferred, else
   body `Parent` / parent-side sub-issue membership, and carries the marker)
   permits Captain-side merge after current-head clean review, required checks,
   and mergeability. Saved `parent_epic_url` is revalidated at resume and the
   merge gate; a different live native parent or moved/removed body Parent drops
   stale inheritance (matching body/saved links must not bypass native moves).
7. Merge failure re-queries authoritative GitHub state. Never blind-retry.
8. No secrets in checkpoints, status, review bodies, or skill notes.
9. If the host cannot continue out of session, perform **one truthful pass**,
   record `one_pass_only`, report the limitation, and stop. Do not spawn an
   unmanaged daemon.

## Commands (deterministic helpers)

Prefer the installed **`hermes-helmet`** console script (works without a source
checkout). Override binaries with `HERMES_HELMET_GH` / `HERMES_HELMET_HERMES` or
`--gh` / `--hermes`. Optional worker transport: `HERMES_HELMET_WORKER_RUNTIME` or
`--worker-runtime` (verbs `ledger-root`, `ledger-watch`, `dispatch-root`, `wait`) so
Captain can adopt the worker ledger/Kanban without a second queue or copied
credentials. Checkpoints default under `~/.hermes-helmet/checkpoints` and store
structured codes only (no raw stderr, no review prose).

```sh
# Read-only status (no mutation)
hermes-helmet status ISSUE_URL --config /path/to/policy.json

# One truthful pass: Captain preflight, adopt/dispatch, PR discovery, checkpoint
hermes-helmet issue ISSUE_URL \
  --config /path/to/policy.json \
  --host-continuation cron|session|none|unknown \
  --one-pass-only   # when host cannot recur

# Discovery only (no dispatch label, no root-task create)
hermes-helmet issue ISSUE_URL --config /path/to/policy.json --no-dispatch

# After posting a formal GitHub review on the exact live head, record outcome
hermes-helmet issue ISSUE_URL \
  --config /path/to/policy.json \
  --record-review clean|changes_requested|blocked \
  --head-sha HEAD_SHA
```

From a checkout before install, `PYTHONPATH=src python -m hermes_helmet.cli …`
is an equivalent developer path; installed copies must use `hermes-helmet`.

Install skill copies into host directories:

```sh
hermes-helmet install-skills --target codex
hermes-helmet install-skills --target claude
hermes-helmet install-skills --target hermes
```

## Procedure

### 1. PREFLIGHT

1. Load authority policy version 2 (`captain_github_login`, `worker_github_login`,
   repositories, labels, budgets, merge markers).
2. Run `hermes-helmet issue ISSUE_URL` (or the Python helper) so identity,
   allowlist, open issue state, ready/dispatch labels, and budgets are checked.
3. Stop with precise status on authority mismatch, closed issue, non-allowlisted
   repo, missing labels, or exhausted budget.

Completion: checkpoint state is past `PREFLIGHT` or terminal with blocker.

### 2. DISPATCH / adopt

1. If a root task already exists in the H1 ledger (issue URL key) or Kanban
   idempotency key, **adopt it**. Never create a duplicate root.
2. If discovery already found a canonical worker PR and no root is recoverable,
   **stop** with `pr_without_recoverable_root` — do not add `dispatch_label` or
   create a second root for work that already has a PR.
3. Otherwise (no root, no PR) ensure `dispatch_label` is present and let the
   shared `create_task_once` path create exactly one root (same idempotency as H1).
4. Do not build a second queue.

Completion: `root_task_id` recorded, or terminal/failed with precise missing-root
reason; no second root for the issue URL.

### 3. WAIT_PR / discover

1. Discover PR from, in order: H1 ledger watch, Kanban run metadata
   (`published_pr` or legacy `pr_url`; valid but different URLs fail closed),
   GitHub timeline / open PRs mentioning the issue or canonical branch
   `automation/<repo>-<n>`.
2. Canonical PR must be authored by the configured worker. Ambiguous open worker
   PRs, replacement-PR disagreement between ledger and Kanban, or non-worker
   authorship → `BLOCKED` with precise reason.
3. Scenario covered: already-open green worker PR (e.g. historical issue #2 /
   PR #10 shape) is discovered and surfaces `REVIEW_HEAD` without a user nudge.

Completion: `pr_url` + head SHA known, or state `WAIT_PR` with truthful status.

### 4. REVIEW_HEAD (agent work)

1. Fetch latest base and **exact** PR head into a **Captain** checkout that is
   not the worker worktree. Do not push the worker branch.
2. Review the diff against the issue acceptance criteria and repo AGENTS/rules.
   Apply the bundled [review and recovery guidance](references/delivery.md).
   Block on a supported material failure or unmet required acceptance; group
   related causes in one pass. Nits and optional stronger proofs stay
   nonblocking and require no owner waiver. For repairs, inspect the new head's
   changes and affected behavior while reusing still-valid evidence.
3. Outcomes:
   - **Material defects / unmet required acceptance** → formal GitHub review `REQUEST_CHANGES` as Captain;
     record `--record-review changes_requested`; state becomes `WAIT_REPAIR`.
   - **Genuinely clean** → formal comment or approve only if policy allows;
     record `--record-review clean`; state `READY`. Do not invent defects.
   - **Unsafe/ambiguous** → `--record-review blocked` with precise reason.
     Clears `clean_head`, sets `BLOCKED`, and is a hard merge stop until a newly
     accepted clean review (or a new head) recovers; unchanged-head resume must
     not promote to `READY` or pass the merge gate.
4. Never copy full review prose into Kanban. GitHub is the review ledger.

Completion: checkpoint `reviewed_head` equals the head you reviewed.

### 5. REQUEST_REPAIR / WAIT_REPAIR

1. After `changes_requested`, do **not** create a Kanban repair card.
2. Rely on the existing H1 poller to observe trusted Captain review and create
   one dependent same-PR repair.
3. On reinvocation, if head changed, invalidate clean state and return to
   `REVIEW_HEAD`. If repair outstanding, stay `WAIT_REPAIR`.

After two failed repairs of one underlying assumption, reassess before posting
another repair-triggering review. If the approach is structurally wrong, use
the bundled recovery procedure to stop and supersede it with a bounded
successor. This requires authority to manage tasks and confirmed quiescence;
it never permits a duplicate root, a second repair relay, or a Captain push to
the worker branch.

Completion: new head appeared, or the precise blocker/recovery handoff is stated.

### 6. READY → MERGE_GATE → optional MERGE → VERIFY_MERGED

1. Clean review of **current** head + required checks green + mergeable.
2. Default: stop for explicit Captain approval (`stop_for_approval`).
3. If issue body has whole-line `Merge when clean: yes` (see authority helpers),
   Captain may merge **once** for that exact head SHA. Policy `local-only`
   worker completion does not treat GitHub `mergeable_state=clean` as independently
   verified required checks; unattended merge then still needs those checks or
   falls back to explicit Captain approval.
4. On merge API failure: re-query PR; if merged, `DONE`; if not, `BLOCKED` with
   GitHub reason. Never loop blind retries.
5. Worker never merges.

### 7. Status and closeout

```sh
hermes-helmet status ISSUE_URL --config policy.json
```

Reports root task, PR, reviewed head, repair state, merge gate, blocker, and
terminal flag without mutation.

Leave the six-question audit in the session/Kanban closeout when you stop:

1. What did it read?
2. What did it write or change?
3. Which tools, accounts, and external systems did it touch?
4. What recommendations or decisions did it produce?
5. Did it stay inside the allowlist?
6. Where did the human approve, reject, or correct it?

## Host continuation

| Host capability | Behavior |
| --- | --- |
| managed process wait | Run one `hermes-helmet wait ISSUE_URL --timeout-seconds 1800`; keep its process handle; after exit 0, run one fresh `helmet-issue` pass and wait again only if still pending |
| cron / scheduler | Fallback when the host cannot keep a process handle: re-invoke `helmet-issue ISSUE_URL` on interval until terminal |
| long session without worker wait | Fallback with bounded backoff; do not spend turns repeatedly reading unchanged state |
| none / unknown | One truthful pass, `--one-pass-only`, report limitation |

The managed wait is read-only and deployment-side. Exit `0` means meaningful
worker state changed; exit `2` means the bounded wait timed out. After every
outcome, pass the returned cursor to the next wait with `--cursor`; this prevents
an already-observed terminal task from waking the host again. Exit `1` is a
transport or contract failure that must be reported. Store only the opaque
cursor and process handle. On timeout, reconcile once and resume the bounded
wait if still pending. Keep unchanged waiting in the process, avoiding repeated
model turns that merely announce it is still waiting. Verify continuation after
host interruption and report the first actionable blocker. Reduce quota use
without treating zero model usage as a delivery requirement;
never copy task bodies, logs, credentials, or review prose into host metadata.

Do not ask the model to sleep, run a shell polling loop, or repeatedly call
`status`. The single wait process observes the existing H1 ledger and Hermes
Kanban. It does not dispatch, review, repair, merge, or create a second watcher.

## Common pitfalls

1. **Running as worker** — orchestration is Captain-only; worker identity fails closed.
2. **Second repair relay** — posting a Kanban repair from this skill duplicates H1.
3. **CI-only thrash** — red checks without verified findings do not start repair.
4. **Stale clean head** — any new SHA clears `clean_head`.
5. **Silent approval** — prose near `Merge when clean` is not authority; whole-line only.
6. **Shared checkout** — never `git push` from Captain into the worker branch tip.
7. **Duplicate root** — always adopt ledger/Kanban before create; never create a
   root after discovering an existing canonical worker PR without a recoverable root.
8. **Blocked review** — explicit `blocked` clears clean authority and sticks on
   the same head until a new clean recording or a new head.

## Verification checklist

- [ ] `hermes-helmet issue` preflight passes only as Captain
- [ ] Reinvocation adopts the same root task and PR
- [ ] Open worker PR discovered without human-supplied PR URL
- [ ] Review anchored to exact head; formal GitHub review posted when actionable
- [ ] No Kanban repair created by this skill
- [ ] `hermes-helmet status` matches checkpoint + live GitHub without writes
- [ ] Merge gate respects default vs `Merge when clean: yes`
- [ ] One-pass hosts report continuation limitation honestly
