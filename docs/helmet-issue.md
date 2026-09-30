# helmet-issue

First-officer single-issue orchestration for Hermes Helmet.

## Follow an issue through acceptance

Give the first officer one GitHub issue URL. It uses the Captain GitHub
identity (`captain_github_login`) to adopt an existing worker task or
dispatch one, follow the linked pull request, review the current change, and
request repairs on that same pull request. Hermes implements and repairs
under `worker_github_login`. The human Captain's policy determines whether a
clean result may be merged. After an interruption, the first officer resumes
from its checkpoint and current GitHub state.

The two identities are two roles, not interchangeable credential profiles.
Hermes writes and repairs as the worker; the first officer independently reviews
the exact head as the Captain. The Captain host stores only the Captain `gh`
profile; preflight fails if it finds the worker login there. A run never
switches tokens, sessions, profiles, or browser state to manufacture the other
identity's approval. A PR authored by anyone other than the configured worker
makes the run terminally failed and is not adopted. Resume requires a compliant
worker-authored PR after conflicting external state is resolved.

```sh
helmet issue ISSUE_URL --config /path/to/policy.json
helmet status ISSUE_URL --config /path/to/policy.json
```

Expected result: a checkpoint past preflight, a recorded root task or a
precise blocker, and later a discovered worker pull request. Next action:
review the current head from a checkout that is not the worker worktree.
When the host cannot stay in session, add `--host-continuation none
--one-pass-only` and stop after one truthful pass.

## Review, repair, and merge

`helmet-issue` drives this loop:

`PREFLIGHT → DISPATCH → WAIT_PR → REVIEW_HEAD → REQUEST_REPAIR → WAIT_REPAIR → … → READY → MERGE_GATE → optional MERGE → VERIFY_MERGED → DONE`

It adopts existing root tasks and pull requests, discovers worker PRs without a
human nudge, refuses to create a second root when a canonical worker PR already
exists without a recoverable root, treats an explicit blocked review as a hard
merge stop (clears clean authority), reviews each head from a separate
first-officer checkout, and posts formal GitHub review findings. The issue
poller (`github-issue-poller`) creates same-PR repair work. The worker never
merges. Default merge mode permits the Captain to merge that exact clean head
without another prompt. A whole-line `Merge when clean: no` on the issue (or an
inheriting parent epic) requires separate explicit approval.

Interactive session hosts ask once before dispatch when no valid persisted
choice exists. `helmet issue --merge-mode …` stores the choice, source, and
authority fingerprint; resumes do not ask again until an authority input
changes. Scheduled runs honor a valid persisted choice and otherwise use the
policy default without asking. An installation configured for explicit Captain
approval cannot be widened.

Canonical implementation selection requires repository + issue association
before DONE: the issue's canonical branch, a GitHub closing keyword aimed at
this issue (``Closes #N``, optional colon, ``owner/repo#N``, or
``Closes https://…/issues/N``), or an existing ledger/Kanban watch URL.
Timeline cross-references, bare ``#N`` mentions, and ordinary body/Markdown
issue links remain discovery leads only — not ownership. Timeline
cross-referenced pull requests are additionally validated against their
canonical repository identity before adoption: valid foreign references are
ignored, references with no usable identity grant no authority, and
contradictory metadata claiming the local repository fails closed.

## Named-request repository enrollment

When the human Captain explicitly asks the first officer to work on or review
a named repository that is not in the static allowlist, that request is
authorization to enroll that exact repository:

```sh
helmet prepare-repo REQUEST_REF --config /path/to/policy.json --purpose work|review
```

`REQUEST_REF` is the exact `owner/name` slug, a canonical issue/PR URL, or a
credential-free Git origin URL; every accepted form is normalized to the exact
slug before the worker-runtime `prepare-repo` transport provisions the
checkout below the configured data root. The sanitized original reference is
retained only in the audit receipt. Enrollment is deterministic and
idempotent: existing allowlisted repositories are no-ops (explicit
`enrollment` restrictions govern new enrollments only and never affect the
static allowlist), receipts persist
atomically and recover after interruption, and repeats re-run the transport
seam so current access and clone validation are never skipped. Stored
receipts are re-validated against the current policy on every lookup, so
owner narrowing, Captain changes, or explicit `enrollment` restrictions
revoke stale receipts. Review-only enrollment never dispatches issues or
grants merge permission; read-only status never enrolls or clones. Optional
consultation is non-blocking here because the explicit named-repo request
already grants authority for that exact repository — not because silence is
consent.

## Non-goals

- No second watcher, webhook, repair relay, queue, or worker checkout
- No copy of review prose into Kanban
- No autonomous repair from CI failure alone
- No worker merge; first-officer merge only under explicit policy

## Package surfaces

| Surface | Role |
| --- | --- |
| `skills/helmet-issue/SKILL.md` | Portable skill (also packaged under `hermes_helmet/bundled_skills/`) |
| `helmet issue ISSUE_URL` | Deterministic preflight/adopt/discover pass + checkpoint |
| `helmet status ISSUE_URL` | Read-only status |
| `helmet prepare-repo REQUEST_REF --purpose work\|review` | Named-request enrollment of one exact repository (Captain-authorized, idempotent) |
| `helmet install-skills` | Install skill into host skill directories |
| `github-issue-poller` | Sole creator of repair Kanban tasks |

## Checkpoint

Non-secret JSON under `~/.hermes-helmet/checkpoints/` by default (override with
`HERMES_HELMET_CHECKPOINT_DIR` or `--checkpoint-dir`), keyed by issue URL digest.
Stores state, root task id, PR URL, reviewed/clean head SHAs, repair round count,
effective merge mode, persisted choice/source, authority fingerprint,
`merge_choice_required`, and structured blocker/operation codes. Never stores tokens, raw
command stdout/stderr, or free-form review prose (GitHub remains the review
ledger). Optional first-officer→worker transport: `--worker-runtime` /
`HERMES_HELMET_WORKER_RUNTIME` (verbs: `ledger-root`, `ledger-watch`,
`dispatch-root`, `wait`) so an absent local ledger can still adopt worker truth
without inventing a second queue. `helmet wait ISSUE_URL` holds one
bounded, read-only worker-side wait and returns a secret-free cursor plus a wake
reason. A long-lived host keeps that process handle, runs one fresh issue pass
after a meaningful wake, and avoids model-driven status loops. Exit `2` is a
clean timeout; exit `1` is a transport or contract failure. Pass the returned
cursor to the next wait after every outcome so an observed terminal state does
not wake repeatedly.

## Legacy checkpoint compatibility

A previous terminal repair still named in the watch is not treated as delivery
for a fresh same-head `changes_requested` cycle; re-recording the same verified
formal review event is idempotent even if intermediate adoption cleared the
transient `pending_repair` flag. Version-1 checkpoints that only carry a
head-only `changes_requested` note still treat that already-counted formal
cycle as spent when watch/repair-body delivery evidence names the same review
event and the head has not yet been migrated to event-scoped history — unless
available checkpoint `updated_at` and formal-review `submitted_at` chronology
proves the review landed after the checkpoint was saved (a genuinely later
cycle the legacy marker cannot cover). Ordinary resume/adoption
(`issue --no-dispatch`) preserves that legacy `updated_at` cutoff while the
head still carries only the head-only marker, so a later formal review remains
distinguishable after adoption and before `record-review`. After migration the
head-only marker no longer exempts a later distinct first-officer
`changes_requested` on the same head (even when the poller has already updated
watch/body to that new event). A terminal repair bound to the currently
recorded review event returns for first-officer re-assessment even when the
worker delivered without changing the head.

## Install targets

| Target | Path |
| --- | --- |
| Codex | `~/.codex/skills/helmet-issue/` |
| Claude Code | `~/.claude/skills/helmet-issue/` |
| Hermes | `~/.hermes/skills/hermes-helmet/helmet-issue/` |

Installation and static validation cover all three targets. The preview's
runtime workflow has been exercised through Codex.

## Status fields

`helmet status` reports:

- root task id
- pull request URL
- reviewed head / clean head
- repair state (`none`, `awaiting-h1-poller`, `outstanding:<id>:<status>`)
- merge gate
- blocker
- terminal flag

## Related

- [authority-schema.md](authority-schema.md)
- [control-loop.md](control-loop.md)
- [quickstart.md](quickstart.md)
