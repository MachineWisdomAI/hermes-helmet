---
name: helmet-write
description: "Coordinate multi-part writing (documents, decks) across section writers and a separate editor through native Kanban, reviewing exact artifact revisions."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
metadata:
  hermes:
    tags: [hermes-helmet, captain, writing, review, kanban]
    related_skills: [helmet-issue, helmet-epic]
---

# helmet-write

## Overview

`helmet-write` is the first-officer skill for writing that several workers
produce together. You plan one Kanban card per contribution plus one editor
card, review each submission as an exact revision, and release the editor only
from accepted revisions. The editor alone assembles the canonical document.
Everything lives in native Kanban tasks and attachments; the `helmet write`
commands only add revision checks. Writers get writing guidance on their cards,
never Captain authority.

## Invocation

The user invokes this skill with its reference and a writing assignment, for
example "write the Q3 update for customers from these notes". That is all.
They do not paste a spec, a handoff, CLI setup commands, plan JSON or
orchestration instructions. You shape the plan, run the workflow and report.
Ask only for what the assignment lacks (audience, purpose, format, sources).
The worker and its provider are configured once beforehand
(`docs/writing-setup.md`); if the readiness checks below fail, say so and stop.

## When to Use

- Several people-hours of writing that splits into parts: a report with
  sections, a deck with slide groups.
- Do not use it for a short edit, a single-author draft, or a review-only
  request. Do those directly with one worker or yourself.
- Do not use it to implement code (`helmet-issue`).

## Steps

0. **Check readiness.** Before the first card, for every profile the plan will
   name (writers, editor, and any replacement you may supersede to), confirm it
   is dispatchable and fresh: it is in the worker's `kanban.dispatch_profiles`,
   `helmet write profile-check` is clean, and its guidance skill is discoverable.
   Check a replacement the same way before you rely on it.
1. **Shape the plan.** Fix audience, purpose, format (`document` or `deck`),
   guidance, sources per part, writer profiles, one editor profile, and
   yourself as coordinator. Only the coordinator must differ from every writer
   and the editor. Writers and the editor may share one writing profile (for
   example `writing`); each works its own card in its own session. For
   assignment rules by format, read
   `references/assignments.md`. Give each contributor only its assigned
   sources, the neighboring passages it needs and the authoritative decisions
   that apply; never the whole source collection. Put audience, voice and the
   guidance pack in the plan's `guidance` so the editor receives them too.
2. **Create the cards.** Write the plan JSON ([schema](references/plan.md)) and
   run `helmet write plan BOARD plan.json`. Re-running the same plan reuses its
   cards; a changed plan is refused.
   Input files named in the plan are attached as native artifacts.
3. **Wait for submissions.** A writer submits with native request-review and
   the card moves to you. Run `helmet write submission BOARD TASK`. It prints
   the revision token and artifact reference; retrieve the file with
   `helmet artifact download`.
4. **Review the exact revision.** Read it against its sources
   (`references/review.md`). Then either:
   - `helmet write request-changes BOARD TASK --revision TOKEN --feedback TEXT`
     with concrete feedback. The card returns to the same writing profile.
   - `helmet write accept BOARD TASK --revision TOKEN --decision TEXT ...`
     recording each editorial decision the editor must honor.
   A stale token is refused; re-read the submission and review the current one.
5. **Escalate after two ineffective iterations.** `request-changes` reports
   `escalation_due`. Change the approach instead of a third pass: clarify the
   sources, or supersede with another writer
   (`helmet write supersede BOARD TASK --editor EDITOR --reason TEXT`).
6. **Release the editor.** When every section is accepted, run
   `helmet write release-editor BOARD EDITOR`. It checks each accepted run was
   made by you and not the writer, attaches the accepted files and decisions,
   and only then starts the editor.
7. **Accept the final.** The editor's completion criterion includes reading
   the complete assembled artifact for continuity and fidelity to the accepted
   inputs before it submits. Review that actual artifact the same way, as an
   exact revision. Report two things separately: structural checks (file opens,
   required text, table values, slide order, notes, embedded image) and your
   source-based editorial assessment. Sizes, elapsed time and word counts are
   descriptive, not targets. The canonical document is
   `helmet write canonical BOARD EDITOR --output PATH`.

## Rules

- One card per contribution. Feedback goes through `request-changes`; a comment
  alone dispatches nothing.
- Never complete a section card yourself and never unblock the editor by hand.
- Revise for clarity and fidelity before compressing length.
- Never promote a raw, unaccepted review comment to a requirement. Only
  `--decision` text you record at acceptance reaches the editor.
- Writers never edit the canonical document or another writer's file.
- Check fresh context with `helmet write context BOARD TASK` and read
  `references/context.md` for what the profile must configure.
- Third-party editors are optional and only through their supported API or MCP.
  See `references/review.md`.

## Failure Modes

Every command fails with `err:CODE: hint` and changes nothing. Common codes:
`stale_revision`, `not_in_review`, `self_review_refused`, `acceptance_unbound`,
`editor_inputs_stale`. `docs/helmet-write.md` lists them all.
