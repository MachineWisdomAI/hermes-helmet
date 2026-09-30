---
name: helmet-epic
description: "Orchestrate a GitHub epic through Hermes Helmet: validate child dependencies, run ready issues with bounded parallelism, and reconcile completion without dispatching the epic root."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
metadata:
  hermes:
    tags: [hermes-helmet, captain, orchestration, github, epic]
    related_skills: [helmet-issue]
---

# helmet-epic

## Overview

`helmet-epic` is the portable first-officer orchestration skill for **one**
GitHub epic: a root/parent issue plus its child-issue dependency graph. The
first officer runs it on the Captain host using `captain_github_login`.
Hermes implements child work under `worker_github_login` and does not run
this skill. The root may come from `grill-me → to-spec → to-tickets`, another
planning process, or hand-authored issues. No document named PRD and no
Jira/Linear Epic object is required.

Given `EPIC_URL`, one invocation loads and validates the graph, computes the
ready frontier, and invokes `helmet-issue` for ready children. Default execution
is bounded parallel (`max_epic_parallelism`, default 2). Independent branches
continue when another child blocks. The epic root is an orchestration record
only: it must **never** receive the dispatch label and is never sent to a worker.
Default behavior leaves the epic open for human closeout.

## When to Use

- Operator says `helmet-epic EPIC_URL` or asks the first officer to run a multi-issue train
- Reinvocation after interruption (resume epic + child checkpoints; re-read GitHub)
- Advancing a parent issue whose children declare `Parent` / `Blocked by` links

Do not use for:

- Single-issue work → `helmet-issue`
- Worker execution inside a Kanban task (worker never runs this skill)
- Inferring order from issue numbers or table row prose

## Accepted delivery outcome

Start from the accepted outcome and current evidence. Use actual Captain/worker
capacity and owner availability; a default parallelism limit does not imply two
available humans. Identify release-critical children, optional work, and final
owner-only actions. Do not add acceptance gates because a reviewer suggested
polish, a higher score, or stronger proof. Adopt explicit user scope changes
with their effect on timing visible.

Probe risky access, identity, topology, and API assumptions through the smallest
real authorized path before dispatching dependent machinery. A synthetic
validator is not a substitute for executing the system. Keep a short current
frontier: outcome, active child/PR, next event, wait handle/cursor, and blocker.
Optional work does not become a release prerequisite; required acceptance
remains required. Any proposed graph change still follows graph acceptance.

Each child uses `helmet-issue`'s bundled review/recovery guidance; external
company skill packs are optional. If a failed child is superseded, preserve its
history and move affected dependency edges to the actual successor under
existing authority. Accept the new graph before dispatch; closing the old PR
or issue does not satisfy its unfinished outcome.

## Graph sources

1. **Native** GitHub sub-issue membership when the API returns them (paginated).
   Native `dependencies/blocked_by` on each child is a **prerequisite edge** source,
   never a membership list for the epic root.
2. **Explicit body fallback** (documented; required when native membership is empty):
   - Children: issues that declare a `Parent` section or `Parent:` line targeting the
     epic (full issue URL or same-repository `#N` / `owner/repo#N`). Allowlisted-repo
     search yields **candidates only** — each hit is kept only after its live Parent
     relationship points at the epic. Ordinary mentions are skipped. `--child` seeds
     still require a strict Parent link (missing/wrong Parent fails closed).
   - Edges: `Blocked by` section or `Blocked by:` line with full URLs or same-repo short
     refs in the declaring issue's repository context. Every declared token is validated;
     mixed malformed lines fail closed. A full issue URL must match the entire path —
     `…/issues/2oops` is not issue `#2`. Native `dependencies/blocked_by` is a prerequisite
     source per child; HTTP 404 is unavailable, while 401/403/transport failures fail closed.
3. Never infer edges from table ordering or narrative “depends on”.
4. Incomplete discovery (failed/partial search, denied native reads) never becomes a
   silently smaller accepted graph. Both `epic` and `epic-status` report nonterminal
   uncertainty on live graph-read failure (historical DONE checkpoint bytes stay put;
   the command must not exit success as completed).

## Hard rules

1. Active GitHub identity MUST be the configured **Captain**
   (`captain_github_login`) and MUST differ from the configured **worker**.
   Fail closed on mismatch. The first officer uses that Captain identity.
2. Identity separation is role separation for every child: the worker authors
   and repairs; the Captain independently reviews the exact head. Never switch
   profiles, tokens, sessions, or authenticated browser state to impersonate
   the other side or manufacture approval. Captain preflight rejects a stored
   worker `gh` profile; the worker credential belongs only in the worker
   runtime. Use each child's `helmet-issue` path; a non-worker-authored PR makes
   that child run terminally `FAILED` until conflicting external state is
   resolved and the accepted issue has a compliant worker-authored PR.
3. Epic root is never dispatch-labeled and never given a worker root task.
4. Human-only children are classified `awaiting_human` only when an **operative**
   title/body instruction declares the gate — including short title markers such as
   `(human only)` / `human-only`, a `## Human-only …` section heading, or a whole-line
   `never dispatch` instruction — not when ordinary issues merely discuss those gates.
5. Validation rejects cycles, self-edges, missing or closed-as-incomplete blockers
   (`state_reason=not_planned` / `duplicate`), non-allowlisted repositories,
   duplicate explicit child seeds, ambiguous multi-Parent sets, and malformed
   operative Parent/Blocked-by tokens.
6. Any change to children or blocking edges pauses **new** dispatch until the
   Captain passes `--accept-graph` for the refreshed fingerprint.
7. Reinvocation resumes existing epic and **active** child runs (including
   `WAIT_REPAIR`) before allocating new ready slots; recovered live roots count
   toward parallelism. Do not create duplicate roots (helmet-issue adopt path).
8. Autonomous merge-after-clean-review is the default. Parent-level whole-line
   `Merge when clean: no` propagates explicit-approval mode to children unless a
   child re-enables autonomy with whole-line `Merge when clean: yes`. Child
   checkpoints store `parent_epic_url` so resume/merge re-reads live parent authority **after**
   revalidating membership: live native parent (`GET …/issues/{n}/parent`) is
   authoritative when present; body `Parent` is the empty-native fallback;
   parent-side sub-issue lists still confirm native-only children. A matching
   saved/body Parent must not bypass a different current native parent.
   Unattended merge-gate status (`unattended_when_clean:awaiting`) is active progress,
   not `awaiting_human`.
   An installation configured `explicit_captain_approval` is a hard ceiling.
9. No secrets in checkpoints, status, or skill notes.
10. If the host cannot continue out of session, one truthful pass with
   `--one-pass-only`, then stop.
11. When policy `captain_model_provider_consent` is true, the Captain's accepted
    epic delegation authorizes every in-scope child task and review to use the
    configured Hermes executor. Do not ask again per child, model, or provider,
    including for configured OpenRouter/open-weight routes. With false or
    omitted, honor explicit consent for this task or obtain it before external
    model use. The flag never authorizes
    secrets, scope expansion, credential/billing changes, or an unconfigured
    provider.

## Commands

Prefer the installed **`helmet`** console script.

```sh
# One truthful epic pass
helmet epic EPIC_URL \
  --config /path/to/policy.json \
  --accept-graph \
  --merge-mode unattended_when_clean|explicit_captain_approval \
  --host-continuation cron|session|none|unknown

# Graph/status only (no child dispatch)
helmet epic EPIC_URL --config policy.json --no-dispatch --accept-graph

# After children/edges change underneath a run
helmet epic EPIC_URL --config policy.json --accept-graph

# Read-oriented status buckets
helmet epic-status EPIC_URL --config policy.json
helmet epic-status EPIC_URL --config policy.json --json

# Optional explicit child seed (still requires Parent → epic)
helmet epic EPIC_URL --child https://github.com/org/repo/issues/N
```

From a checkout before install: `PYTHONPATH=src python -m hermes_helmet.cli …`.
Installed copies must use `helmet`.

Install skill copies:

```sh
helmet install-skills --target codex
helmet install-skills --target claude
helmet install-skills --target hermes
```

## Procedure

### 1. PREFLIGHT

1. Load authority policy version 2 (Captain/worker, allowlists, labels, budgets,
   `max_epic_parallelism`).
2. On an interactive session, if status reports `merge_choice_required: true`,
   ask once before dispatch, rerun with `--merge-mode`, and propagate that
   persisted choice to every child. Do not ask again unless policy, authority
   markers, validated parent, or the accepted graph changes. Scheduled runs
   honor a valid persisted choice and otherwise use the policy default without
   a question.
3. Report the effective epic and child Captain windows, worker wall-clock/turn
   quotas, repair limit, merge mode, and continuation mechanism at startup from
   `helmet epic-status --json` (`details.budget_announcement`, `details.budgets`,
   `details.budget_window`). Announce actual explicit overrides rather than
   promising six days when policy is shorter. These are bounded elapsed windows,
   not uninterrupted CPU or unlimited model spend. Do not repeat an unchanged
   announcement during managed waits. Keep one event-driven waiter per child
   with a 1,800-second wait bound.
4. Verify observed GitHub login is Captain and differs from worker.
5. Load epic root: must be open; must **not** carry `dispatch_label`.
6. Stop on authority mismatch, closed root, missing label config, or bad budgets.

### 2. LOAD + VALIDATE GRAPH

1. Prefer native sub-issue/dependency APIs when non-empty.
2. Else discover children via Parent links (search + optional `--child` seeds).
3. Parse Blocked-by URLs; load external blockers on allowlisted repos.
4. Reject cycles, self-edges, incomplete closed blockers, ambiguous parents,
   non-allowlisted repos.
5. Fingerprint `root + children + edges`. On fingerprint change vs accepted
   checkpoint, set `GRAPH_CHANGED` and pause new dispatch until `--accept-graph`.

### 3. CLASSIFY CHILDREN

Buckets (always re-read GitHub + child checkpoints before acting):

| Bucket | Meaning |
| --- | --- |
| completed | Closed-complete or helmet-issue `DONE` |
| active | In-flight helmet-issue non-terminal states |
| ready | Open, blockers satisfied, not yet active |
| blocked | Open child waiting on incomplete blockers |
| failed | helmet-issue `FAILED` or invalid closed state |
| awaiting_human | Human-only, blocked review, or merge stop-for-approval |

### 4. DISPATCH FRONTIER

1. Ready frontier = ready children, capped by `max_parallelism - active`.
2. Default: continue independent branches when another fails/blocks.
3. For each frontier URL invoke `helmet-issue` helpers (`helmet issue` /
   `run_preflight_and_adopt`) with epic body for merge inheritance.
4. Never pass the epic root URL to helmet-issue dispatch.

### 5. STATUS + CLOSEOUT

```sh
helmet epic-status EPIC_URL --config policy.json
```

Leave the epic **open** by default. Report completed/active/ready/blocked/failed/
awaiting_human. Check the requested outcome against delivered evidence; an empty
PR queue is not program completion. State code readiness, required runtime
acceptance, and publication authority separately. Leave the six-question audit
in session/Kanban closeout when you stop.

## Host continuation

| Host capability | Behavior |
| --- | --- |
| managed process wait | For each active child (bounded by `max_parallelism`), keep one `helmet wait CHILD_URL --timeout-seconds 1800` process handle; rerun the epic pass after the first meaningful wake |
| cron / scheduler | Fallback when managed waiting cannot resume the host; re-invoke `helmet-epic EPIC_URL` until children settle |
| long session without worker wait | Loop with bounded backoff; do not repeatedly read unchanged state |
| none / unknown | One truthful pass, `--one-pass-only`, report limitation |

After the first meaningful wake, reconcile the entire graph once. Keep other
bounded child wait handles until their natural wake or timeout; never start a
duplicate handle for the same child. If a completed handle no longer belongs to
the active frontier, discard its result after it exits. Pass each child's
returned cursor to its next wait after every outcome, including a meaningful
wake. On timeout, reconcile once and resume only still-pending waits. Keep
unchanged waiting out of repeated model turns; notify on actionable change and
verify resumption after interruption. Do not add a second heartbeat or observer
to watch the waiter. Do not use model-managed sleeps or a shell polling loop.

## Common pitfalls

1. **Dispatching the epic root** — orchestration only; never add dispatch_label.
2. **Inferring order from tables** — only Parent / Blocked-by URL edges count.
3. **Skipping graph acceptance** — child/edge edits must pause until `--accept-graph`.
4. **Running as worker** — Captain-only; worker identity fails closed.
5. **Human-only children** — e.g. public-launch issues stay awaiting_human.
6. **Duplicate child roots** — helmet-issue adopt path; do not force re-create.
7. **Switching identities for approval** — keep worker authorship and Captain
   review separate; a second credential profile is not a substitute for the
   Helmet issue workflow.

## Verification checklist

- [ ] `helmet epic` preflight passes only as Captain
- [ ] Root never receives dispatch_label
- [ ] Body fallback Parent / Blocked-by links validate; cycles rejected
- [ ] Linear graphs invoke helmet-issue in dependency order
- [ ] Parallel graphs run at most `max_epic_parallelism` (default 2)
- [ ] Blocked branch does not stop unrelated ready children (default)
- [ ] Graph change → GRAPH_CHANGED until `--accept-graph`
- [ ] Reinvocation resumes without duplicate roots
- [ ] Epic merge marker propagates; child narrow works
- [ ] `epic-status` reports all six buckets; epic remains open
- [ ] install-skills validates Codex, Claude, Hermes targets
