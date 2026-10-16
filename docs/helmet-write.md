# Iterative writing with revision-bound review

`helmet write` and the `helmet-write` skill let a first officer coordinate section
writers and a separate editor through the existing Kanban. Each contribution is
one native card. Review and acceptance name an exact artifact revision. The
editor alone assembles the canonical document.

It adds no queue, scheduler, database, or content validator. Task state, runs,
comments and attachments are native Kanban. Artifact bytes use the
[task-artifacts](task-artifacts.md) handoff.

Short edits and review-only requests bypass this workflow: use one worker or do
the edit directly.

## Flow

```
plan ──► section cards (one per contribution)      editor card (held: blocked)
              │ writer: request-review → coordinator
              ▼
        submission (revision token) ── request-changes ──► same card, same writer
              │ accept (exact token, editorial decisions)
              ▼
        all sections accepted ── release-editor ──► editor receives exact files + decisions
                                                      │ editor submits → coordinator accepts
                                                      ▼
                                              canonical document (editor card)
```

```sh
helmet write plan        BOARD plan.json
helmet write submission  BOARD TASK
helmet write history     BOARD TASK
helmet write request-changes BOARD TASK --revision TOKEN --feedback TEXT
helmet write accept      BOARD TASK --revision TOKEN [--decision TEXT ...] [--note TEXT]
helmet write release-editor BOARD EDITOR
helmet write supersede   BOARD TASK --editor EDITOR --reason TEXT [--assignee PROFILE]
helmet write canonical   BOARD EDITOR [--output PATH]
helmet write context     BOARD TASK
helmet write context-plugin PLUGINS_DIR
helmet write profile-check CONFIG_YAML
```

All commands print JSON, never file contents. Failures are `err:CODE: hint`.
Commands that talk to the board use `HERMES_HELMET_HERMES` for the `hermes`
binary and `HERMES_HELMET_WORKER_RUNTIME` for the artifact transport, like the
other coordinator commands. See the [plan schema](../skills/helmet-write/references/plan.md).

## Roles and authority

| Role | Does | Never does |
| --- | --- | --- |
| First officer (coordinator) | Plans, reviews, accepts, requests changes, releases and supersedes. Runs as its own Hermes profile. | Writes sections or the final document. |
| Section writer | Writes one contribution and submits it with native request-review to the coordinator. | Accepts, unblocks, creates cards, edits another card or the canonical document. |
| Editor | Assembles the canonical document from the accepted inputs on its own card. | Uses attachments that are not listed as current accepted inputs. |

The coordinator profile must differ from every writer and from the editor;
`plan` refuses a plan where it does not. Document workers get writing guidance
only: audience, purpose, format, voice, sources, the submit/revise loop, and the
editor's fidelity and cut rules. Their cards contain no Captain, merge, policy or
orchestration instructions.

## Revision binding

A submission's revision token is
`helmet-write-revision:v1:BOARD:TASK:RUN:ATTACHMENT_ID:SHA256`: the card, the
native review-request run, the staged attachment and its checksum.

- `request-changes` and `accept` take the token. The card is re-read first; if
  the card is not awaiting review, has a newer submission, or the token names
  another card, the command refuses with `stale_revision`, `not_in_review` or
  `revision_task_mismatch` and changes nothing.
- `request-changes` uses the native `reopen-review` (review → ready, assignee
  restored from the review request, feedback recorded as a comment). That is real
  dispatch: the same card returns to the same writing profile. A new session
  continues from the card's body (sources), the previous attachment and the
  feedback in native task context. The comment alone does not dispatch.
- `accept` completes the card from review with run metadata naming the revision,
  artifact reference and editorial decisions, then re-derives the accepted
  revision from native runs and fails if it is not the one reviewed.
- Every upload is a new attachment; earlier revisions stay readable
  (`history`, `helmet artifact download`). A later file with the same name cannot
  replace an accepted revision, because acceptance names the attachment id and
  checksum.

**Residual native window.** Native `reopen-review` and `complete` take no
revision argument, so a competing session could change the card between the
re-read and the call. `accept` detects a different outcome afterwards
(`acceptance_superseded`/`acceptance_unverified`), and `release-editor` refuses
any section whose accepted revision is not its latest submission. The gap is
not closable above the native API. Run one coordinator per project.

## Self-approval and release

Native Kanban lets a running writer complete its own card, which would promote
dependents. `release-editor` therefore does not trust dependency state:

- the editor card is created `blocked`, which stays blocked even when all parents
  are done, and only `release-editor` unblocks it;
- each section's accepted run must carry a `helmet_write` record for the card's
  latest submitted revision, made by a profile other than the submitting writer;
- the accepted file must still be retrievable by attachment id and checksum.

Failures are `acceptance_unbound`, `self_approval`, `acceptance_superseded` and
`accepted_artifact_missing`; the editor stays blocked.

`release-editor` then copies each accepted file to the editor card as
`CONTRIBUTION-rRUN-NAME`, writes the revisions and editorial decisions into the
card's "Current accepted inputs" block, and unblocks. The editor reads native
parent results and that block. Its acceptance is refused with
`editor_inputs_stale` if any input is no longer the section's accepted revision.

## Reopening or superseding

Native Kanban has no CLI to reopen a finished card. `supersede` creates a
replacement card from the old one (same sources, an added note naming the reason
and the prior accepted revision), links it to the editor in the old card's place,
comments on the old card, clears the editor's accepted inputs, and re-gates the
editor (returning it from review if it was awaiting acceptance). It refuses when
the editor is running (`editor_running`) or done (`editor_done`). The editor never
sees an input whose card was superseded.

## Fresh task context

A writing card should receive its sources, guidance, the previous artifact and
feedback, and the runtime and authority instructions Hermes requires. It should
not receive unrelated work history or persistent retrieval.

| Source of unrelated context | Native behavior | What Hermes Helmet does |
| --- | --- | --- |
| "Recent work by @profile" in task context | Always appended by `build_worker_context`. No flag disables it; a new session id does not. | The `helmet-writing-context` plugin (`helmet write context-plugin`) removes that section from the `kanban_show` result for cards created by `plan` only. |
| `MEMORY.md` / `USER.md` | Profile-level (`memory.memory_enabled`, `memory.user_profile_enabled`). Not per task. | Cannot be set per task. `profile-check` reports it. Use a dedicated writing profile with both off. |
| External memory provider | Profile-level (`memory.provider`). | Same: `profile-check` reports it; the adopter configures the profile. |
| SOUL/AGENTS and authority instructions | Required runtime context. | Kept. `--ignore-rules` is not used. |

Install for the writing profile: run `helmet write context-plugin
PROFILE_HOME/plugins`, then `hermes plugins enable helmet-writing-context` and
set the profile's memory settings. `helmet write profile-check
PROFILE_HOME/config.yaml` must report no findings.

What is inspectable: `helmet write context BOARD TASK` prints native task context
(for a writing card this still shows the role history the plugin removes at the
tool seam), and `hermes prompt-size --json` shows built-in memory and user-profile
sizes. The regression test runs the real context builder in an isolated home with
an unrelated task marker and a memory marker, shows the marker present natively,
shows the plugin transform excludes it while the assigned sources, feedback and
revision history stay, and shows memory size drop to zero under the profile
settings.

**Remaining gaps (explicit).**

- Hermes hooks fail open: with the plugin missing or disabled, role history is
  not removed. `profile-check` reports a profile that does not enable it.
- Persistent retrieval cannot be scoped per task; isolation depends on the
  dedicated profile. A native per-task switch would replace this.
- The plugin acts where the worker reads context (`kanban_show`). It does not
  change the dispatcher's spawn prompt, which is only "work kanban task ID".

## Integration (W4/W5)

1. Install skills: `helmet install-skills` (or the Claude/Codex plugin). The
   `helmet-write` skill is Captain-side and is reserved, so company skill packs
   cannot ship a skill by that name to workers.
2. Provide the coordinator profile name, the editor profile, and writer profiles
   as Hermes profiles on the board. Use a writing profile (reasoning effort per
   the deployment's role policy) for writers and editor; the coordinator is the
   first officer's own profile, not a writer.
3. Configure the writing profiles: memory and provider off, plugin installed and
   enabled, `profile-check` clean.
4. Run `plan`, then drive `submission` → `request-changes`/`accept`, then
   `release-editor`, then review the editor's submission and use `canonical`.
5. Real-host acceptance (a DOCX or PPTX through the real Docker boundary) stays
   with the deployment adapter, as in [task-artifacts](task-artifacts.md). The
   repository tests use the native CLI in an isolated home and in-memory
   artifacts; they do not exercise a deployed adapter or a live board.

Third-party editors are optional and only through their supported API or MCP, with
output returned as an ordinary artifact that is reviewed like any other.

## Error codes

| Code | Meaning |
| --- | --- |
| `revision_malformed`, `revision_task_mismatch` | Not a revision token, or it names another board or card |
| `stale_revision`, `not_in_review` | The card moved on since that revision |
| `submission_missing`, `submission_artifact_missing`, `submission_artifact_ambiguous` | The review request carries no single native attachment |
| `self_review_refused`, `review_owner_mismatch` | Reviewer is the writer, or the submission was routed to someone other than the planned coordinator |
| `feedback_required`, `feedback_too_long`, `decision_invalid` | Review text rules |
| `request_changes_unverified`, `acceptance_unverified` | Native call did not leave the card as expected |
| `not_accepted`, `acceptance_unbound`, `acceptance_superseded`, `self_approval`, `accepted_artifact_missing` | Section is not validly accepted |
| `editor_not_blocked`, `editor_running`, `editor_done`, `editor_inputs_mismatch`, `editor_inputs_stale` | Editor state or inputs do not allow the action |
| `not_a_writing_card`, `wrong_card_role` | Card was not made by `plan`, or the verb does not apply to it |
| `plan_invalid`, `plan_field_invalid` | The plan file is malformed |
| `kanban_command_failed`, `kanban_response_invalid` | The `hermes kanban` call failed or returned something unexpected |

Artifact errors (`artifact_*`) are described in [task-artifacts](task-artifacts.md).
