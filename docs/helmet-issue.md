# helmet-issue

Captain-side single-issue orchestration for Hermes Helmet.

## Follow an issue through acceptance

Give the Captain one GitHub issue URL. It adopts an existing worker task or
dispatches one, follows the linked pull request, reviews the current change,
and requests repairs on that same pull request. The human owner's policy
determines whether a clean result may be merged. After an interruption, the
Captain resumes from its checkpoint and current GitHub state.

## Review and resume behavior

`helmet-issue` takes one GitHub issue URL and drives the resumable loop:

`PREFLIGHT → DISPATCH → WAIT_PR → REVIEW_HEAD → REQUEST_REPAIR → WAIT_REPAIR → … → READY → MERGE_GATE → optional MERGE → VERIFY_MERGED → DONE`

It adopts existing root tasks and pull requests, discovers worker PRs without a
human nudge, refuses to create a second root when a canonical worker PR already
exists without a recoverable root, treats an explicit blocked review as a hard
merge stop (clears clean authority), reviews each head from a separate Captain
checkout, posts formal GitHub review findings, and relies on the existing H1
poller for same-PR repair. A previous terminal repair still named in the watch
is not treated as delivery for a fresh same-head `changes_requested` cycle;
re-recording the same verified formal review event is idempotent even if
intermediate adoption cleared the transient `pending_repair` flag. Legacy
version-1 checkpoints that only carry a head-only `changes_requested` note
still treat that already-counted formal cycle as spent when watch/repair-body
delivery evidence names the same review event and the head has not yet been
migrated to event-scoped history — unless available checkpoint `updated_at` and
formal-review `submitted_at` chronology proves the review landed after the
checkpoint was saved (a genuinely later cycle the legacy marker cannot cover).
Ordinary resume/adoption (`issue --no-dispatch`) preserves that legacy
`updated_at` cutoff while the head still carries only the head-only marker, so a
later formal review remains distinguishable after adoption and before
`record-review`. After migration the head-only marker no longer exempts a later
distinct Captain `changes_requested` on the same head (even when the poller has
already updated watch/body to that new event). A terminal repair bound to the
currently recorded review event returns for Captain re-assessment even when the
worker delivered without changing the head.

Canonical implementation selection requires repository + issue association
before DONE: the issue's canonical branch, a GitHub closing keyword aimed at
this issue (``Closes #N``, optional colon, ``owner/repo#N``, or
``Closes https://…/issues/N``), or an existing ledger/Kanban watch URL.
Timeline cross-references, bare ``#N`` mentions, and ordinary body/Markdown
issue links remain discovery leads only — not ownership.

## Non-goals

- No second watcher, webhook, repair relay, queue, or worker checkout
- No copy of review prose into Kanban
- No autonomous repair from CI failure alone
- No worker merge; Captain merge only under explicit policy

## Package surfaces

| Surface | Role |
| --- | --- |
| `skills/helmet-issue/SKILL.md` | Portable skill (also packaged under `hermes_helmet/bundled_skills/`) |
| `helmet issue ISSUE_URL` | Deterministic preflight/adopt/discover pass + checkpoint |
| `helmet status ISSUE_URL` | Read-only status |
| `helmet install-skills` | Install skill into host skill directories |
| H1 `github_issue_poller` | Sole creator of repair Kanban tasks |

## Checkpoint

Non-secret JSON under `~/.hermes-helmet/checkpoints/` by default (override with
`HERMES_HELMET_CHECKPOINT_DIR` or `--checkpoint-dir`), keyed by issue URL digest.
Stores state, root task id, PR URL, reviewed/clean head SHAs, repair round count,
merge mode, and structured blocker/operation codes. Never stores tokens, raw
command stdout/stderr, or free-form review prose (GitHub remains the review
ledger). Optional Captain→worker transport: `--worker-runtime` /
`HERMES_HELMET_WORKER_RUNTIME` (verbs: `ledger-root`, `ledger-watch`,
`dispatch-root`, `wait`) so an absent local ledger can still adopt worker truth
without inventing a second queue. `helmet wait ISSUE_URL` holds one
bounded, read-only worker-side wait and returns a secret-free cursor plus a wake
reason. A long-lived host keeps that process handle, runs one fresh issue pass
after a meaningful wake, and avoids model-driven status loops. Exit `2` is a
clean timeout; exit `1` is a transport or contract failure. Pass the returned
cursor to the next wait after every outcome so an observed terminal state does
not wake repeatedly.

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
