# Reviewing a submission

Read this when you have a revision to review or a hard case to settle.

## What to check

- **Factual fidelity.** Every claim traces to the card's listed sources. Flag a
  claim the sources do not support; do not repair it yourself.
- **Connected passages.** Read neighbouring parts together: transitions,
  repeated claims, terminology, tense and order. The editor repeats this across
  contributions, so record what you notice as editorial decisions.
- **Intentional cuts.** A cut is acceptable when the writer or editor says what
  was cut and why. An unexplained loss of a sourced fact is a defect.
- **Revision before length compression.** If the text is too long, ask for
  clearer revision first. Ask for shortening only once the points are resolved.

## Feedback

Name the passage, the problem and the wanted change. One request-changes round
covers every point you have. A vague round wastes the two iterations you have
before escalating.

## After two ineffective iterations

If two rounds did not fix the same issue, stop. Reassign the part to another
writer with `helmet write supersede`, or clarify the sources and restate the
brief. Do not continue the loop.

## Pending cards

Follow the general rule in
[Supervise pending work](../../helmet-issue/references/host-continuation.md#supervise-pending-work):
yield on the host's completion delivery or one native schedule and reconcile the
board once per meaningful wake. Writing-specific points:

- **Progress is substance.** Judge a card by what exists relative to the
  assignment's difficulty: a substantive draft or attachment, a resolved source
  question, or identifiable necessary preparation such as reading long sources.
  A heartbeat, tool-call count or superficial file change is not progress.
- **Pick an observation point** that fits the card (a short section differs from
  a source-heavy editor pass) and the continuation you already have. Waiting
  past it needs new evidence, a concrete next step and a new observation point.
  There is no universal timeout, reviewer deadline or polling loop.
- **Absent or unrelated progress.** Comment on the same card with the problem and
  next step; a comment is stored and read by a later run, so do not say it
  interrupted the worker. If that is not enough, an authorized operator may
  retry the card with reclaim, as the shared rule describes. That is a same-card
  retry: it does not hold the card, and the card can run again. Check the
  `reclaimed` event for the deployed termination outcome rather than assuming the
  old worker stopped.
- **Replacing a contribution.** `helmet write supersede` creates a replacement
  card and re-gates the editor. It refuses while the editor is running
  (`editor_running`) and it neither blocks nor cancels the old section card, so an
  old contribution that is still running or gets retried can continue beside its
  replacement. Reclaim does not close that gap, and reclaiming an editor does not
  prevent it from being dispatched again before `supersede` runs. If the controls
  you are authorized to use cannot hold or retire the old execution, report that
  blocker and the decision needed, and do not claim the replacement is isolated.
  Recovery of a stalled editor or of unfinished output stays deferred: keep the
  accepted inputs, the old card's attachments and any unfinished work as they are,
  never edit task metadata or the board database, and name any refused control.

## Reopening or superseding an accepted part

Accepted work can be replaced if a source changes. `helmet write supersede`
creates a replacement card, takes the old one out of the editor's inputs, clears
the editor's accepted inputs and holds it. Accept the replacement and release
the editor again; the editor receives the new exact revision.

## Optional third-party editors

A third-party editing tool may help a writer or editor, but only through its
supported API or MCP server, with its output returned as a normal artifact that
you review like any other. Never script a tool's interface, copy its private
state, or let it write to a card directly.
