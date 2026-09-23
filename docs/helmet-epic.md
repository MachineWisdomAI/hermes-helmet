# helmet-epic

Captain-side multi-issue (epic) orchestration for Hermes Helmet.

## Purpose

`helmet-epic` takes one GitHub epic/parent issue URL and drives:

`PREFLIGHT → load/validate graph → classify children → dispatch ready frontier → WAITING/DONE`

It invokes `helmet-issue` for each ready child with bounded parallelism
(`max_epic_parallelism`, default 2), continues independent branches when another
child blocks, and never dispatch-labels the epic root. The epic remains open by
default for human closeout.

## Graph model

| Source | Role |
| --- | --- |
| Native GitHub sub-issues (membership) | Preferred when the membership API returns results |
| Native per-child `dependencies/blocked_by` | Prerequisite edges only (not membership) |
| Explicit body `Parent` / `Blocked by` links | Documented fallback (full URLs or same-repo `#N`) |

Never infer edges from table row order or narrative prose; same-repo `#N` refs resolve only inside operative Parent/Blocked-by sections.

Validation rejects cycles, self-edges, missing or closed-as-incomplete blockers,
non-allowlisted repositories, duplicate explicit child seeds, malformed operative links, and
ambiguous multi-Parent sets.

## Non-goals

- No second watcher, webhook, or epic-specific worker queue
- No dispatch of the epic root or human-only children
- No automatic close of the epic root
- No force-push; child merge remains helmet-issue / Captain policy

## Package surfaces

| Surface | Role |
| --- | --- |
| `skills/helmet-epic/SKILL.md` | Portable skill (also under `bundled_skills/`) |
| `hermes-helmet epic EPIC_URL` | One truthful graph/frontier/child-invoke pass |
| `hermes-helmet epic-status EPIC_URL` | Status buckets without child dispatch |
| `hermes-helmet issue` | Per-child orchestration (invoked by epic) |

## Checkpoint

Non-secret JSON under `~/.hermes-helmet/checkpoints/` (override with
`HERMES_HELMET_CHECKPOINT_DIR` or `--checkpoint-dir`), keyed as `epic__…`.
Stores graph fingerprint, accepted fingerprint, active/completed/failed buckets,
parallelism, and structured notes. Never stores tokens or free-form secrets.

Graph fingerprint changes set state `GRAPH_CHANGED` and pause new child dispatch
until `--accept-graph`.

## Status buckets

`hermes-helmet epic-status` reports:

- completed
- active
- ready
- blocked
- failed
- awaiting_human

plus root_open, fingerprint, max_parallelism, blocker, and terminal flag.

## Merge inheritance

`Merge when clean: yes` on the epic body grants unattended-when-clean mode to
children via `merge_authority_for(..., epic_body=…)`. A child whole-line
`Merge when clean: no` narrows back to explicit Captain approval. Default mode
still stops each merge for explicit approval when no marker is present.

## Install targets

| Target | Path |
| --- | --- |
| Codex | `~/.codex/skills/helmet-epic/` |
| Claude Code | `~/.claude/skills/helmet-epic/` |
| Hermes | `~/.hermes/skills/hermes-helmet/helmet-epic/` |

Static validation covers all three. Runtime dogfood of the skill loop is
required in Codex only (private follow-on).

## Related

- [helmet-issue.md](helmet-issue.md)
- [authority-schema.md](authority-schema.md)
- [control-loop.md](control-loop.md)
- [quickstart.md](quickstart.md)
