# helmet-epic

First-officer multi-issue (epic) orchestration for Hermes Helmet.

## Coordinate dependent issues

Use an epic when a body of work spans several issues. The first officer
identifies which issues are ready, runs independent work within the
configured limit, and follows each child's implementation, review, and
acceptance through `helmet-issue`. You, the human Captain, retain the final
closeout of the parent issue.

```sh
helmet epic EPIC_URL --config /path/to/policy.json --accept-graph
helmet epic-status EPIC_URL --config /path/to/policy.json
```

Expected result: a validated graph fingerprint, a ready frontier, and
in-flight or completed children. Next action: wait on active children, then
rerun the epic pass. Leave the parent issue open unless you close it
yourself.

`helmet-epic` takes one GitHub epic/parent issue URL and drives:

`PREFLIGHT → load/validate graph → classify children → dispatch ready frontier → WAITING/DONE`

It invokes `helmet-issue` for each ready child with bounded parallelism
(`max_epic_parallelism`, default 2), continues independent branches when another
child blocks, and never dispatch-labels the epic root.

## Graph model

| Source | Role |
| --- | --- |
| Native GitHub sub-issues (membership) | Preferred when the membership API returns results |
| Native per-child `dependencies/blocked_by` | Prerequisite edges only (not membership) |
| Explicit body `Parent` / `Blocked by` links | Documented fallback (full URLs or same-repo `#N`) |

Native membership avoids GitHub Search entirely on both execution and status
passes. When the native child list is empty or unavailable (HTTP 404), body-link
discovery searches only the epic's repository (three queries regardless of how
many repositories are enrolled). Cross-repository children must be native
sub-issues or supplied with repeated `--child ISSUE_URL` arguments on each pass.
Explicit children still require a valid live Parent relationship; enrollment
alone does not make a repository part of an epic's discovery scope.

For existing body-only plans spanning repositories, or plans mixing native
children with body-only children even in the same repository, link the body-only
children as native sub-issues or supply them explicitly before continuing. Changes to an accepted
graph still require `--accept-graph`. Parent relationships, dependencies, and
membership authorization are revalidated live; this optimization does not cache
a previously accepted graph as current truth.

Never infer edges from table row order or narrative prose; same-repo `#N` refs resolve only inside operative Parent/Blocked-by sections.

Validation rejects cycles, self-edges, missing or closed-as-incomplete blockers,
repositories that are neither allowlisted nor covered by a valid named-request
enrollment receipt, duplicate explicit child seeds, malformed operative links, and
ambiguous multi-Parent sets. Review-only enrolled children are never dispatched.

## Non-goals

- No second watcher, webhook, or epic-specific worker queue
- No dispatch of the epic root or human-only children
- No automatic close of the epic root
- No force-push; child merge remains helmet-issue / Captain policy

## Package surfaces

| Surface | Role |
| --- | --- |
| `skills/helmet-epic/SKILL.md` | Portable skill (also under `bundled_skills/`) |
| `helmet epic EPIC_URL` | One truthful graph/frontier/child-invoke pass |
| `helmet epic-status EPIC_URL` | Status buckets without child dispatch |
| `helmet issue` | Per-child orchestration (invoked by epic) |

## Checkpoint

Non-secret JSON under `~/.hermes-helmet/checkpoints/` (override with
`HERMES_HELMET_CHECKPOINT_DIR` or `--checkpoint-dir`), keyed as `epic__…`.
Stores graph fingerprint, accepted fingerprint, active/completed/failed buckets,
parallelism, original `started_at`, and structured notes. Never stores tokens or free-form secrets.

Graph fingerprint changes set state `GRAPH_CHANGED` and pause new child dispatch
until `--accept-graph`.

## Status buckets

`helmet epic-status` reports:

- completed
- active
- ready
- blocked
- failed
- awaiting_human (includes human-only children and routed children whose code
  merged but whose tracking issue is not yet accepted; see
  [issue-routing.md](issue-routing.md))

plus root_open, fingerprint, max_parallelism, blocker, terminal flag, and
effective budgets (`started_at`, `deadline`, epic/child windows, worker quota).

## Merge inheritance

Unattended-after-clean-review mode applies to children by default through
`merge_authority_for(..., epic_body=…)`. `Merge when clean: no` on the epic body
narrows children to explicit Captain approval; a child whole-line
`Merge when clean: yes` can re-enable autonomous merge for that child.
An installation-wide explicit-approval policy remains a hard ceiling. On an
interactive session, `helmet epic --merge-mode …` persists one choice and
propagates it to children. Scheduled runs honor a valid persisted choice and
otherwise use the policy default without asking. Authority or accepted-graph
changes invalidate an unattended choice.

## Host continuation

Pending children use native completion delivery only when it automatically
resumes the First Officer. Otherwise, one native schedule covers the whole
epic at the policy's `first_officer_check_interval_minutes` cadence (default
five minutes), with explicit per-run overrides saved in that schedule. Child
passes reuse the epic's continuation. Each wake reconciles the entire frontier
once and yields when unchanged. Scheduled continuation replaces child waiters;
it stops when no authorized work remains, the original budget expires, or human
action is required. See the shared
[host continuation procedure](../skills/helmet-issue/references/host-continuation.md).

## Install targets

| Target | Path |
| --- | --- |
| Codex | `~/.codex/skills/helmet-epic/` |
| Claude Code | `~/.claude/skills/helmet-epic/` |
| Hermes | `~/.hermes/skills/hermes-helmet/helmet-epic/` |

Installation and static validation cover all three targets. The preview's
runtime workflow has been exercised through Codex.

## Related

- [helmet-issue.md](helmet-issue.md)
- [authority-schema.md](authority-schema.md)
- [control-loop.md](control-loop.md)
- [quickstart.md](quickstart.md)
