#!/usr/bin/env python3
"""Revision-bound coordinator review for iterative writing on native Kanban.

A first officer plans one card per written contribution plus one editor card,
reviews each submission, and releases the editor only from accepted revisions.
Everything is stored in native Kanban tasks, runs, comments and attachments.
There is no queue, scheduler, database or content validator here.

Native operations used (``hermes kanban``): ``create``, ``show --json``,
``reopen-review`` (review -> ready, assignee restored, feedback recorded as a
comment: this is real dispatch), ``complete`` (acceptance from ``review``),
``block``/``unblock``, ``edit``, ``comment``, ``link``/``unlink``.

Native review operations take no revision argument, so staleness is enforced
here: every request-changes and acceptance names the exact revision token
(board, task, review run, attachment id, SHA-256), and the card is re-read
immediately before acting and verified afterwards. A short native window
between the re-read and the call remains; the post-call verification reports it
instead of hiding it (``docs/helmet-write.md``).

Self-approval is refused in two places. A reviewer that is also the submitting
profile cannot act. And an editor is released only if each section's accepted
run belongs to a different profile than its submitting run and names the
section's latest submitted revision, so a writer that completes its own card
(which native Kanban allows for a running card) never releases the editor.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Mapping, Protocol, Sequence

from hermes_helmet import task_artifacts as ta
from hermes_helmet.helmet_issue import DEFAULT_HERMES, HelmetIssueError, Runner

CARD_MARKER = "<!-- helmet-write:card:v1 -->"
REVISION_PREFIX = "helmet-write-revision:v1:"
ROLE_SECTION = "section"
ROLE_EDITOR = "editor"
FORMATS = ("document", "deck")
MAX_FEEDBACK = 8000
ESCALATION_AFTER = 2

_META_RE = re.compile(r"<!-- helmet-write:meta (\{.*?\}) -->", re.S)
_INPUTS_RE = re.compile(
    r"<!-- helmet-write:inputs:begin -->.*?<!-- helmet-write:inputs:end -->", re.S
)
_INPUTS_DATA_RE = re.compile(r"<!-- helmet-write:inputs (\[.*?\]) -->", re.S)
_REVISION_RE = re.compile(
    r"^helmet-write-revision:v1:"
    r"(?P<board>[a-z0-9][a-z0-9_-]{0,63}):"
    r"(?P<task>[A-Za-z0-9][A-Za-z0-9_-]{0,63}):"
    r"(?P<run>[1-9][0-9]{0,17}):"
    r"(?P<att>[1-9][0-9]{0,17}):"
    r"(?P<sha>[0-9a-f]{64})$"
)
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_HINTS: Mapping[str, str] = {
    "revision_malformed": "expected helmet-write-revision:v1:BOARD:TASK:RUN:ATTACHMENT_ID:SHA256 from `helmet write submission`",
    "revision_task_mismatch": "the revision belongs to a different board or task than the one named",
    "stale_revision": "the card has a newer submission or is no longer in review; read the current submission and review that revision",
    "not_in_review": "only a card awaiting coordinator review can be reviewed",
    "submission_missing": "the card has no review request; the writer must submit through request-review",
    "submission_artifact_missing": "the submission carries no native attachment; the file must be inside the card workspace when requesting review",
    "submission_artifact_ambiguous": "a submission must carry exactly one deliverable file",
    "self_review_refused": "the reviewer profile is the submitting profile; the coordinator must be a different profile",
    "review_owner_mismatch": "the submission was not routed to the planned coordinator profile; send it back for resubmission",
    "not_a_writing_card": "the card was not created by `helmet write plan`",
    "wrong_card_role": "this operation does not apply to that kind of card",
    "feedback_required": "request-changes needs concrete feedback text",
    "feedback_too_long": "feedback is limited to 8000 characters",
    "decision_invalid": "editorial decisions must be short non-empty strings",
    "kanban_command_failed": "the Hermes Kanban command failed; nothing was recorded",
    "kanban_response_invalid": "the Hermes Kanban command returned an unexpected shape",
    "request_changes_unverified": "the card did not return to its writer after reopen; inspect it before continuing",
    "request_changes_conflict": "the card moved to a newer submission while changes were being requested; the feedback was recorded against a different revision and the card was annotated; review the newer revision",
    "reviewer_identity_mismatch": "the Hermes runtime used for this command is not the planned coordinator profile that holds the card; run it as the coordinator",
    "acceptance_reviewer_mismatch": "the accepted run does not belong to the planned coordinator; the acceptance is not valid review",
    "acceptance_unverified": "the card did not record the accepted revision; inspect it before releasing the editor",
    "acceptance_profile_conflict": "the accepting profile equals the submitting profile; the acceptance is not valid review",
    "not_accepted": "the card has no accepted revision",
    "acceptance_unbound": "the completed run does not name an accepted revision of this card",
    "acceptance_superseded": "a newer submission exists after the accepted revision",
    "self_approval": "the accepted run belongs to the submitting profile",
    "accepted_artifact_missing": "the accepted revision is no longer retrievable from the card",
    "editor_not_blocked": "only an editor card held in blocked can be released",
    "editor_running": "the editor is running; reclaim it natively before changing its inputs",
    "editor_done": "the editor already completed; supersede it and the section before rebuilding",
    "editor_inputs_mismatch": "the editor parents do not match the planned contributions",
    "editor_inputs_stale": "the editor was built from a revision that is no longer the accepted one",
    "plan_invalid": "the plan is not a valid writing plan",
    "plan_changed": "this project already has cards with different content; a plan is not amended in place. Rerun the identical plan, use `helmet write supersede` for a part, or plan a new project key",
    "plan_field_invalid": "a plan field is missing or malformed",
}


class WritingReviewError(RuntimeError):
    """Fixed-code failure. It never carries card text, file content or output."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"err:{code}: {_HINTS.get(code, code)}{suffix}")


# --------------------------------------------------------------------------
# Revision identity.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    board: str
    task_id: str
    run_id: int
    attachment_id: int
    sha256: str

    @property
    def token(self) -> str:
        return (
            f"{REVISION_PREFIX}{self.board}:{self.task_id}:"
            f"{self.run_id}:{self.attachment_id}:{self.sha256}"
        )

    @property
    def artifact_token(self) -> str:
        return (
            f"{ta.REFERENCE_PREFIX}{self.board}:{self.task_id}:"
            f"{self.attachment_id}:{self.sha256}"
        )


def parse_revision(token: object) -> Revision:
    match = _REVISION_RE.fullmatch(token.strip()) if isinstance(token, str) else None
    if match is None:
        raise WritingReviewError("revision_malformed")
    return Revision(
        board=match.group("board"),
        task_id=match.group("task"),
        run_id=int(match.group("run")),
        attachment_id=int(match.group("att")),
        sha256=match.group("sha"),
    )


@dataclass(frozen=True)
class Submission:
    revision: Revision
    artifact: ta.ArtifactRef
    implementer: str
    reviewer: str
    summary: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision.token,
            "task_id": self.revision.task_id,
            "run_id": self.revision.run_id,
            "implementer": self.implementer,
            "reviewer": self.reviewer,
            "summary": self.summary,
            "artifact": self.artifact.to_public_dict(),
        }


# --------------------------------------------------------------------------
# Native Kanban seam.
# --------------------------------------------------------------------------


class KanbanBoard(Protocol):
    """The native operations this workflow needs, nothing more."""

    def show(self, task_id: str) -> dict[str, Any]: ...

    def context(self, task_id: str) -> str: ...

    def list_cards(self) -> list[dict[str, Any]]: ...

    def acting_profile_owns(self, task_id: str) -> bool: ...

    def create(
        self,
        title: str,
        *,
        body: str,
        assignee: str,
        parents: Sequence[str] = (),
        idempotency_key: str | None = None,
        blocked: bool = False,
    ) -> str: ...

    def reopen_review(self, task_id: str, reason: str) -> None: ...

    def complete(self, task_id: str, summary: str, metadata: Mapping[str, Any]) -> None: ...

    def block(self, task_id: str, reason: str) -> None: ...

    def unblock(self, task_id: str, reason: str) -> None: ...

    def edit_body(self, task_id: str, body: str) -> None: ...

    def comment(self, task_id: str, text: str) -> None: ...

    def link(self, parent_id: str, child_id: str) -> None: ...

    def unlink(self, parent_id: str, child_id: str) -> None: ...


class HermesKanban:
    """``hermes kanban`` over an injectable runner (no output is ever echoed)."""

    def __init__(self, board: str, runner: Runner, *, hermes: str = DEFAULT_HERMES) -> None:
        self.board = board
        self._runner = runner
        self._hermes = hermes

    def _run(self, *args: str) -> str:
        try:
            return self._runner.run([self._hermes, "kanban", "--board", self.board, *args])
        except HelmetIssueError as exc:
            raise WritingReviewError("kanban_command_failed") from exc

    def show(self, task_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._run("show", task_id, "--json"))
        except ValueError as exc:
            raise WritingReviewError("kanban_response_invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
            raise WritingReviewError("kanban_response_invalid")
        return payload

    def context(self, task_id: str) -> str:
        return self._run("context", task_id)

    def _rows(self, *args: str) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self._run("list", *args, "--json"))
        except ValueError as exc:
            raise WritingReviewError("kanban_response_invalid") from exc
        if not isinstance(rows, list):
            raise WritingReviewError("kanban_response_invalid")
        return [row for row in rows if isinstance(row, dict)]

    def list_cards(self) -> list[dict[str, Any]]:
        """Every non-archived card on the board (id, title, body, assignee, status)."""

        return self._rows()

    def acting_profile_owns(self, task_id: str) -> bool:
        """Is the runtime that executes these commands the profile holding the review?

        Native ``list --mine`` filters by the profile the Hermes runtime itself
        runs as, so it is true here only when that profile is the card's
        assignee. Asking the runtime that will run the next command is the only
        identity that counts; the local environment can differ from it.
        """

        rows = self._rows("--mine", "--status", "review")
        return any(row.get("id") == task_id for row in rows)

    def create(
        self,
        title: str,
        *,
        body: str,
        assignee: str,
        parents: Sequence[str] = (),
        idempotency_key: str | None = None,
        blocked: bool = False,
    ) -> str:
        args = ["create", title, "--body", body, "--assignee", assignee, "--json"]
        for parent in parents:
            args += ["--parent", parent]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        if blocked:
            args += ["--initial-status", "blocked"]
        try:
            created = json.loads(self._run(*args))
        except ValueError as exc:
            raise WritingReviewError("kanban_response_invalid") from exc
        task_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise WritingReviewError("kanban_response_invalid")
        return task_id

    def reopen_review(self, task_id: str, reason: str) -> None:
        self._run("reopen-review", task_id, "--reason", reason)

    def complete(self, task_id: str, summary: str, metadata: Mapping[str, Any]) -> None:
        self._run(
            "complete", task_id, "--summary", summary, "--metadata", json.dumps(dict(metadata))
        )

    def block(self, task_id: str, reason: str) -> None:
        self._run("block", task_id, reason, "--kind", "needs_input")

    def unblock(self, task_id: str, reason: str) -> None:
        self._run("unblock", task_id, "--reason", reason)

    def edit_body(self, task_id: str, body: str) -> None:
        self._run("edit", task_id, "--body", body)

    def comment(self, task_id: str, text: str) -> None:
        self._run("comment", task_id, text)

    def link(self, parent_id: str, child_id: str) -> None:
        self._run("link", parent_id, child_id)

    def unlink(self, parent_id: str, child_id: str) -> None:
        self._run("unlink", parent_id, child_id)


# --------------------------------------------------------------------------
# Card contract (what a document worker is told, and what the coordinator reads).
# --------------------------------------------------------------------------


def _meta_text(meta: Mapping[str, object]) -> str:
    text = json.dumps(dict(meta), sort_keys=True, separators=(",", ":"))
    return text.replace(">", "\\u003e").replace("<", "\\u003c")


def parse_card_meta(body: object) -> dict[str, Any]:
    match = _META_RE.search(body) if isinstance(body, str) else None
    if match is None or CARD_MARKER not in str(body):
        raise WritingReviewError("not_a_writing_card")
    try:
        meta = json.loads(match.group(1))
    except ValueError as exc:
        raise WritingReviewError("not_a_writing_card") from exc
    if not isinstance(meta, dict) or meta.get("role") not in (ROLE_SECTION, ROLE_EDITOR):
        raise WritingReviewError("not_a_writing_card")
    return meta


def _require_text(plan: Mapping[str, Any], key: str, limit: int = 4000) -> str:
    value = plan.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise WritingReviewError("plan_field_invalid", key)
    return value.strip()


def _require_profile(value: object, name: str) -> str:
    if not isinstance(value, str) or _PROFILE_RE.fullmatch(value) is None:
        raise WritingReviewError("plan_field_invalid", name)
    return value


def _bullets(items: object, name: str, limit: int = 4000) -> str:
    if items is None:
        return "(none supplied)"
    if not isinstance(items, list) or not all(
        isinstance(item, str) and item.strip() and len(item) <= limit for item in items
    ):
        raise WritingReviewError("plan_field_invalid", name)
    return "\n".join(f"- {item.strip()}" for item in items) or "(none supplied)"


_SHARED = """\
Project: {title}
Format: {fmt}
Audience: {audience}
Purpose: {purpose}

Guidance:
{guidance}
"""

_SECTION_RULES = """\
## How this card works

You write exactly one contribution. You do not plan, assign, review, accept,
unblock or create cards, and you never edit another card or the final document.
Write only claims the sources below support; mark any gap instead of filling it.
Keep the voice and terms of the guidance above.

1. Work in your card workspace. Save the deliverable as one file there.
2. Submit with `kanban_request_review`: `reviewer` is `{coordinator}` and
   `metadata` is `{{"artifacts": ["<path of the one file>"]}}`. Do not call
   `kanban_complete` on this card; only the reviewer accepts it.
3. If the card returns with `CHANGES REQUESTED`, read the feedback comment and
   the previous attachment in your task context, revise that same file as a new
   file, and submit again. Address each point; do not rewrite unrelated text or
   shorten to fit before the points are resolved.
"""

_EDITOR_RULES = """\
## How this card works

You are the only assembler of the canonical {fmt}. Section writers never edit
it. Assemble it only from the artifacts listed under "Current accepted inputs"
below. If that block is missing, comment and block this card: it was started
before its inputs were accepted. Attachments not listed there are superseded
history; do not use them.

Do this in order:
1. Read the accepted inputs and their editorial decisions; honor them.
2. Review connected passages across contributions (transitions, repeated
   claims, terminology, tense, order), not each part alone.
3. Keep every factual claim faithful to its section's sources. Do not add
   claims. Record each intentional cut and why in your summary.
4. Revise for clarity before compressing length.
5. Before you submit, read the complete assembled {fmt} from the first line
   to the last (every table, every slide with its notes) for continuity and for
   fidelity to the accepted inputs. Reading each part alone is not enough.
6. Save the result as one file in your workspace and submit it with
   `kanban_request_review` (`reviewer` `{coordinator}`, `metadata`
   `{{"artifacts": ["<path>"]}}`). Do not call `kanban_complete`.
"""


def _meta_line(meta: Mapping[str, object]) -> str:
    return f"{CARD_MARKER}\n<!-- helmet-write:meta {_meta_text(meta)} -->"


def render_section_body(plan: Mapping[str, Any], section: Mapping[str, Any]) -> str:
    meta = {
        "role": ROLE_SECTION,
        "project": plan["project"],
        "contribution": section["key"],
        "coordinator": plan["coordinator"],
        "implementer": section["assignee"],
        "format": plan["format"],
        "plan": plan.get("fingerprint"),
    }
    return "\n".join(
        [
            _meta_line(meta),
            "",
            f"# Contribution: {section['title']}",
            "",
            _SHARED.format(
                title=plan["title"],
                fmt=plan["format"],
                audience=plan["audience"],
                purpose=plan["purpose"],
                guidance=plan["guidance"],
            ),
            "## Assignment",
            section["brief"],
            "",
            "## Sources (the only basis for factual claims)",
            _bullets(section.get("sources"), "sources"),
            "",
            _SECTION_RULES.format(coordinator=plan["coordinator"]),
        ]
    )


def render_editor_body(plan: Mapping[str, Any], contributions: Sequence[str]) -> str:
    editor = plan["editor"]
    meta = {
        "role": ROLE_EDITOR,
        "project": plan["project"],
        "contributions": list(contributions),
        "coordinator": plan["coordinator"],
        "implementer": editor["assignee"],
        "format": plan["format"],
        "plan": plan.get("fingerprint"),
    }
    return "\n".join(
        [
            _meta_line(meta),
            "",
            f"# Editor: {plan['title']}",
            "",
            _SHARED.format(
                title=plan["title"],
                fmt=plan["format"],
                audience=plan["audience"],
                purpose=plan["purpose"],
                guidance=plan["guidance"],
            ),
            "## Contributions to assemble",
            "\n".join(f"- {key}" for key in contributions),
            "",
            _EDITOR_RULES.format(fmt=plan["format"], coordinator=plan["coordinator"]),
            _render_inputs_block([]),
        ]
    )


def _render_inputs_block(inputs: Sequence[Mapping[str, Any]]) -> str:
    lines = ["<!-- helmet-write:inputs:begin -->", "## Current accepted inputs", ""]
    if not inputs:
        lines.append("(none yet: this card is released only after every section is accepted)")
    for item in inputs:
        lines += [
            f"### {item['contribution']}",
            f"- Input file (attached to this card): {item['input_file']}",
            f"- Accepted revision: {item['revision']}",
        ]
        decisions = item.get("editorial_decisions") or []
        if decisions:
            lines.append("- Editorial decisions:")
            lines += [f"  - {decision}" for decision in decisions]
        lines.append("")
    data = json.dumps(list(inputs), sort_keys=True, separators=(",", ":"))
    data = data.replace(">", "\\u003e").replace("<", "\\u003c")
    lines += [f"<!-- helmet-write:inputs {data} -->", "<!-- helmet-write:inputs:end -->"]
    return "\n".join(lines)


def _replace_inputs(body: str, inputs: Sequence[Mapping[str, Any]]) -> str:
    block = _render_inputs_block(inputs)
    if _INPUTS_RE.search(body):
        return _INPUTS_RE.sub(lambda _m: block, body)
    return body.rstrip() + "\n\n" + block


def recorded_inputs(body: object) -> list[dict[str, Any]]:
    match = _INPUTS_DATA_RE.search(body) if isinstance(body, str) else None
    if match is None:
        return []
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Reading a card.
# --------------------------------------------------------------------------


def _runs(show: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    runs = show.get("runs")
    if not isinstance(runs, list):
        raise WritingReviewError("kanban_response_invalid")
    return sorted((r for r in runs if isinstance(r, dict)), key=lambda r: int(r.get("id", 0)))


def _review_runs(show: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [r for r in _runs(show) if r.get("outcome") == "review_requested"]


def _run_artifact_names(run: Mapping[str, Any]) -> set[str]:
    metadata = run.get("metadata")
    paths = metadata.get("artifacts") if isinstance(metadata, dict) else None
    if not isinstance(paths, list):
        return set()
    return {os.path.basename(p) for p in paths if isinstance(p, str) and p.strip()}


def _artifact_for_run(
    run: Mapping[str, Any], listing: Sequence[ta.ArtifactRef]
) -> ta.ArtifactRef | None:
    names = _run_artifact_names(run)
    found = [a for a in listing if a.role == ta.ROLE_ORIGINAL and a.filename in names]
    if not names or not found:
        return None
    if len(found) > 1:
        raise WritingReviewError("submission_artifact_ambiguous")
    return found[0]


class WritingReview:
    """The coordinator's operations on one board."""

    def __init__(self, board: str, kanban: KanbanBoard, transport: ta.ArtifactTransport) -> None:
        self.board = board
        self.kanban = kanban
        self.transport = transport

    # -- reading ----------------------------------------------------------

    def _listing(self, task_id: str) -> list[ta.ArtifactRef]:
        return ta.list_artifacts(self.transport, board=self.board, task_id=task_id)

    def _revision_of(self, run: Mapping[str, Any], artifact: ta.ArtifactRef) -> Revision:
        return Revision(self.board, artifact.task_id, int(run["id"]), artifact.attachment_id, artifact.sha256)

    def submission(self, task_id: str) -> Submission:
        """The submission currently awaiting review, with its exact revision."""

        show = self.kanban.show(task_id)
        return self._submission_from(show, task_id)

    def _submission_from(self, show: Mapping[str, Any], task_id: str) -> Submission:
        task = show["task"]
        parse_card_meta(task.get("body"))
        if task.get("status") != "review":
            raise WritingReviewError("not_in_review", str(task.get("status")))
        runs = _review_runs(show)
        if not runs:
            raise WritingReviewError("submission_missing")
        last = runs[-1]
        artifact = _artifact_for_run(last, self._listing(task_id))
        if artifact is None:
            raise WritingReviewError("submission_artifact_missing")
        return Submission(
            revision=self._revision_of(last, artifact),
            artifact=artifact,
            implementer=str(last.get("profile") or ""),
            reviewer=str(task.get("assignee") or ""),
            summary=str(last.get("summary") or "")[:400],
        )

    def history(self, task_id: str) -> dict[str, object]:
        """Every submitted revision, in order, plus review decisions and status."""

        show = self.kanban.show(task_id)
        task = show["task"]
        meta = parse_card_meta(task.get("body"))
        listing = self._listing(task_id)
        revisions = []
        for run in _review_runs(show):
            artifact = _artifact_for_run(run, listing)
            revisions.append(
                {
                    "run_id": run["id"],
                    "submitted_by": run.get("profile"),
                    "summary": str(run.get("summary") or "")[:400],
                    "revision": self._revision_of(run, artifact).token if artifact else None,
                    "artifact": artifact.token if artifact else None,
                }
            )
        raw_comments = show.get("comments")
        comments: list[Any] = raw_comments if isinstance(raw_comments, list) else []
        changes = [
            c for c in comments
            if isinstance(c, dict) and REVISION_PREFIX in str(c.get("body", ""))
            and "CHANGES REQUESTED" in str(c.get("body", ""))
        ]
        return {
            "task_id": task_id,
            "role": meta["role"],
            "status": task.get("status"),
            "revisions": revisions,
            "changes_requested": len(changes),
            "escalation_due": len(changes) >= ESCALATION_AFTER,
        }

    # -- review decisions ---------------------------------------------------

    def _current(self, task_id: str, revision: str) -> tuple[Submission, dict[str, Any], dict[str, Any]]:
        wanted = parse_revision(revision)
        if wanted.board != self.board or wanted.task_id != task_id:
            raise WritingReviewError("revision_task_mismatch")
        show = self.kanban.show(task_id)
        meta = parse_card_meta(show["task"].get("body"))
        current = self._submission_from(show, task_id)
        if current.revision != wanted:
            raise WritingReviewError("stale_revision")
        if current.reviewer == current.implementer or (
            os.environ.get("HERMES_PROFILE", "") == current.implementer
        ):
            raise WritingReviewError("self_review_refused")
        if current.reviewer != meta.get("coordinator"):
            raise WritingReviewError("review_owner_mismatch")
        # The card is held by the planned coordinator. The Hermes runtime that
        # will execute the next native call must be that same profile: ask that
        # runtime (it filters by its own profile), not the local environment.
        if not self.kanban.acting_profile_owns(task_id):
            raise WritingReviewError("reviewer_identity_mismatch")
        return current, show, meta

    def _settled(self, task_id: str, revision: str) -> tuple[Submission, dict[str, Any], dict[str, Any]]:
        """Re-check the revision after the slow artifact transfer, just before the native call."""

        current, show, meta = self._current(task_id, revision)
        ta.fetch_artifact(self.transport, current.artifact, board=self.board, task_id=task_id)
        return self._current(task_id, revision)

    def request_changes(self, task_id: str, revision: str, feedback: str) -> dict[str, object]:
        """Send the exact reviewed revision back to its writing profile."""

        text = feedback.strip() if isinstance(feedback, str) else ""
        if not text:
            raise WritingReviewError("feedback_required")
        if len(text) > MAX_FEEDBACK:
            raise WritingReviewError("feedback_too_long")
        current, _show, _meta = self._settled(task_id, revision)
        self.kanban.reopen_review(
            task_id, f"on {current.revision.token}: {text}"
        )
        after_show = self.kanban.show(task_id)
        # The native call names no revision. Verify the transition acted on the
        # reviewed run, not on a newer submission that landed in the window.
        latest_runs = _review_runs(after_show)
        latest_run = int(latest_runs[-1]["id"]) if latest_runs else 0
        if latest_run != current.revision.run_id:
            try:
                self.kanban.comment(
                    task_id,
                    f"SUPERSEDED FEEDBACK: the preceding review comment was written for "
                    f"{current.revision.token}, not for the newer submission (run {latest_run}) "
                    "that was returned with it. It does not apply to that submission; "
                    "resubmit it unchanged unless other feedback applies.",
                )
            except WritingReviewError:
                pass
            raise WritingReviewError("request_changes_conflict", f"newer run {latest_run}")
        after = after_show["task"]
        if after.get("status") not in ("ready", "todo") or after.get("assignee") != current.implementer:
            raise WritingReviewError("request_changes_unverified")
        record = self.history(task_id)
        return {
            "task_id": task_id,
            "reviewed_revision": current.revision.token,
            "status": after.get("status"),
            "assignee": after.get("assignee"),
            "changes_requested": record.get("changes_requested"),
            "escalation_due": record.get("escalation_due"),
        }

    def accept(
        self,
        task_id: str,
        revision: str,
        *,
        decisions: Sequence[str] = (),
        note: str = "",
    ) -> dict[str, object]:
        """Accept the exact revision. Editorial decisions travel with it."""

        cleaned = _clean_decisions(decisions)
        current, _show, meta = self._settled(task_id, revision)
        if meta["role"] == ROLE_EDITOR:
            self._require_editor_inputs_current(task_id, _show["task"].get("body"))
        record = {
            "schema": 1,
            "decision": "accepted",
            "revision": current.revision.token,
            "artifact": current.revision.artifact_token,
            "implementer": current.implementer,
            "role": meta["role"],
            "contribution": meta.get("contribution"),
            "editorial_decisions": cleaned,
        }
        summary = f"Accepted {current.revision.token}"
        if note.strip():
            summary += f": {note.strip()[:400]}"
        self.kanban.complete(task_id, summary, {"helmet_write": record})
        verified = self.accepted_revision(task_id)
        if verified.revision != current.revision:
            raise WritingReviewError("acceptance_unverified")
        return {
            "task_id": task_id,
            "accepted_revision": verified.revision.token,
            "artifact": verified.artifact.token,
            "editorial_decisions": cleaned,
        }

    # -- accepted state ---------------------------------------------------

    def accepted_revision(self, task_id: str) -> "AcceptedRevision":
        """The accepted revision, re-derived from native runs; or a fixed error."""

        show = self.kanban.show(task_id)
        task = show["task"]
        meta = parse_card_meta(task.get("body"))
        if task.get("status") != "done":
            raise WritingReviewError("not_accepted", str(task.get("status")))
        runs = _runs(show)
        completed = [r for r in runs if r.get("outcome") == "completed"]
        if not completed:
            raise WritingReviewError("acceptance_unbound")
        accepting = completed[-1]
        metadata = accepting.get("metadata")
        record = metadata.get("helmet_write") if isinstance(metadata, dict) else None
        if not isinstance(record, dict) or record.get("decision") != "accepted":
            raise WritingReviewError("acceptance_unbound")
        revision = parse_revision(record.get("revision"))
        if revision.board != self.board or revision.task_id != task_id:
            raise WritingReviewError("acceptance_unbound")
        submitted = [r for r in _review_runs(show) if int(r["id"]) == revision.run_id]
        if not submitted or int(submitted[0]["id"]) > int(accepting["id"]):
            raise WritingReviewError("acceptance_unbound")
        if int(_review_runs(show)[-1]["id"]) != revision.run_id:
            raise WritingReviewError("acceptance_superseded")
        if accepting.get("profile") == submitted[0].get("profile"):
            raise WritingReviewError("self_approval")
        # Native completion from review records the card's assignee (the planned
        # coordinator when review was routed correctly) as the run's profile.
        # Acting identity itself is checked before the call (`_current`).
        if accepting.get("profile") != meta.get("coordinator"):
            raise WritingReviewError("acceptance_reviewer_mismatch")
        artifact = next(
            (a for a in self._listing(task_id)
             if a.attachment_id == revision.attachment_id and a.sha256 == revision.sha256),
            None,
        )
        if artifact is None:
            raise WritingReviewError("accepted_artifact_missing")
        decisions = record.get("editorial_decisions")
        return AcceptedRevision(
            revision=revision,
            artifact=artifact,
            contribution=str(meta.get("contribution") or ""),
            implementer=str(submitted[0].get("profile") or ""),
            reviewer=str(accepting.get("profile") or ""),
            editorial_decisions=[d for d in decisions if isinstance(d, str)]
            if isinstance(decisions, list) else [],
        )

    def canonical(self, editor_id: str, destination: str | None = None) -> dict[str, object]:
        """The canonical document: the editor card's accepted revision, verified."""

        meta = parse_card_meta(self.kanban.show(editor_id)["task"].get("body"))
        if meta["role"] != ROLE_EDITOR:
            raise WritingReviewError("wrong_card_role")
        accepted = self.accepted_revision(editor_id)
        self._require_editor_inputs_current(editor_id, self.kanban.show(editor_id)["task"].get("body"))
        if destination:
            ta.save_artifact(
                self.transport, accepted.artifact, destination,
                board=self.board, task_id=editor_id,
            )
        return {"editor_task_id": editor_id, "revision": accepted.revision.token,
                "artifact": accepted.artifact.to_public_dict()}

    # -- editor gating ----------------------------------------------------

    def _section_states(self, editor_show: Mapping[str, Any], meta: Mapping[str, Any]) -> list["AcceptedRevision"]:
        parents = editor_show.get("parents")
        if not isinstance(parents, list) or not parents:
            raise WritingReviewError("editor_inputs_mismatch")
        accepted = [self.accepted_revision(str(parent)) for parent in parents]
        if sorted(a.contribution for a in accepted) != sorted(meta.get("contributions", [])):
            raise WritingReviewError("editor_inputs_mismatch")
        return accepted

    def _require_editor_inputs_current(self, editor_id: str, body: object) -> None:
        editor_show = self.kanban.show(editor_id)
        meta = parse_card_meta(editor_show["task"].get("body"))
        current = {a.contribution: a.revision.token for a in self._section_states(editor_show, meta)}
        built = {i.get("contribution"): i.get("revision") for i in recorded_inputs(body)}
        if built != current:
            raise WritingReviewError("editor_inputs_stale")

    def release_editor(self, editor_id: str) -> dict[str, object]:
        """Hand the editor its accepted revisions, then let it start.

        Each section must be accepted by a profile other than its writer, on
        its latest submission, with the artifact still retrievable. The
        accepted bytes are copied to the editor card as inputs, verified.
        """

        show = self.kanban.show(editor_id)
        task = show["task"]
        meta = parse_card_meta(task.get("body"))
        if meta["role"] != ROLE_EDITOR:
            raise WritingReviewError("wrong_card_role")
        if task.get("status") != "blocked":
            raise WritingReviewError("editor_not_blocked", str(task.get("status")))
        accepted = self._section_states(show, meta)
        existing = {a.filename: a for a in self._listing(editor_id)}
        inputs = []
        for item in sorted(accepted, key=lambda a: a.contribution):
            name = f"{item.contribution}-r{item.revision.run_id}-{item.artifact.filename}"
            have = existing.get(name)
            if have is None or have.sha256 != item.artifact.sha256:
                fetched = ta.fetch_artifact(
                    self.transport, item.artifact, board=self.board, task_id=item.revision.task_id
                )
                have = ta.upload_artifact_bytes(
                    self.transport, board=self.board, task_id=editor_id,
                    name=name, data=fetched.data, media_type=item.artifact.media_type,
                )
            inputs.append(
                {
                    "contribution": item.contribution,
                    "task_id": item.revision.task_id,
                    "revision": item.revision.token,
                    "input_file": have.filename,
                    "input_artifact": have.token,
                    "editorial_decisions": item.editorial_decisions,
                }
            )
        self.kanban.edit_body(editor_id, _replace_inputs(str(task.get("body") or ""), inputs))
        self.kanban.unblock(editor_id, "all sections accepted; inputs attached")
        return {
            "editor_task_id": editor_id,
            "status": self.kanban.show(editor_id)["task"].get("status"),
            "inputs": inputs,
        }

    def supersede_section(
        self, editor_id: str, section_id: str, *, reason: str, assignee: str | None = None
    ) -> dict[str, object]:
        """Replace an accepted (or in-flight) contribution and gate the editor again.

        Native Kanban has no CLI that reopens a finished card, so the accepted
        card stays as history and a replacement card, linked to the editor in
        its place, carries the assignment, the prior revision and the reason.
        """

        why = reason.strip() if isinstance(reason, str) else ""
        if not why:
            raise WritingReviewError("feedback_required")
        editor_show = self.kanban.show(editor_id)
        editor = editor_show["task"]
        editor_meta = parse_card_meta(editor.get("body"))
        if editor_meta["role"] != ROLE_EDITOR:
            raise WritingReviewError("wrong_card_role")
        status = editor.get("status")
        if status == "running":
            raise WritingReviewError("editor_running")
        if status == "done":
            raise WritingReviewError("editor_done")
        old_show = self.kanban.show(section_id)
        old = old_show["task"]
        old_meta = parse_card_meta(old.get("body"))
        if old_meta["role"] != ROLE_SECTION or section_id not in (editor_show.get("parents") or []):
            raise WritingReviewError("editor_inputs_mismatch")
        prior = ""
        try:
            prior = self.accepted_revision(section_id).revision.token
        except WritingReviewError:
            prior = "none accepted"
        owner = _require_profile(assignee or old_meta["implementer"], "assignee")
        if status == "review":
            # An editor submission built on this input is no longer acceptable.
            self.kanban.reopen_review(
                editor_id, f"input {old_meta['contribution']} superseded: {why[:500]}"
            )
        if status != "blocked":
            self.kanban.block(editor_id, f"input {old_meta['contribution']} superseded")
        new_meta = {**old_meta, "implementer": owner}
        body = _META_RE.sub(lambda _m: f"<!-- helmet-write:meta {_meta_text(new_meta)} -->", str(old.get("body") or ""))
        body += (
            f"\n\n## Supersedes {section_id}\n"
            f"Reason: {why[:1000]}\nPrior accepted revision: {prior}\n"
            "Start from the sources above and the prior accepted file attached to the "
            "superseded card; the reviewer decides what carries over.\n"
        )
        new_id = self.kanban.create(
            str(old.get("title") or old_meta["contribution"]),
            body=body, assignee=owner,
            idempotency_key=f"helmet-write:{old_meta['project']}:{old_meta['contribution']}:sup:{section_id}",
        )
        self.kanban.unlink(section_id, editor_id)
        self.kanban.link(new_id, editor_id)
        self.kanban.comment(section_id, f"SUPERSEDED by {new_id}: {why[:1000]}")
        self.kanban.edit_body(editor_id, _replace_inputs(str(editor.get("body") or ""), []))
        return {"superseded": section_id, "replacement": new_id,
                "editor_task_id": editor_id,
                "editor_status": self.kanban.show(editor_id)["task"].get("status")}

    # -- planning ---------------------------------------------------------

    def plan(self, plan: Mapping[str, Any], *, base_dir: str = ".") -> dict[str, object]:
        """Create (or find, by idempotency key) the section cards and the editor.

        Nothing becomes runnable until the whole plan is in place. Sources are
        read and checked first. Section cards are created held (blocked), their
        input files are attached and verified, the editor is created and linked,
        and only then are the held sections released. A failure at any point
        leaves cards that no dispatcher will claim; rerunning the identical plan
        finishes the job. A plan that differs from the one already recorded for
        the project is refused (``plan_changed``) before anything is created.
        """

        checked = validate_plan(plan)
        # 1. Read every source first: a missing or oversized input fails here,
        #    before any card exists.
        sources: dict[str, list[tuple[str, bytes]]] = {}
        for section in checked["sections"]:
            loaded = []
            for path in section.get("input_files") or []:
                loaded.append(ta._read_source(os.path.join(base_dir, path)))
            sources[section["key"]] = loaded
        fingerprint = _plan_fingerprint(checked, sources)
        checked = {**checked, "fingerprint": fingerprint}
        self._require_same_plan(checked["project"], fingerprint)

        # 2. Held section cards, then their verified inputs.
        section_ids: dict[str, str] = {}
        for section in checked["sections"]:
            task_id = self.kanban.create(
                f"{checked['title']}: {section['title']}",
                body=render_section_body(checked, section),
                assignee=section["assignee"],
                idempotency_key=f"helmet-write:{checked['project']}:{section['key']}",
                blocked=True,
            )
            section_ids[section["key"]] = task_id
            for base, data in sources[section["key"]]:
                self._attach_input(task_id, base, data)

        # 3. The held editor, parented on exactly these sections.
        keys = [s["key"] for s in checked["sections"]]
        editor_id = self.kanban.create(
            f"{checked['title']}: editor",
            body=render_editor_body(checked, keys),
            assignee=checked["editor"]["assignee"],
            parents=[section_ids[k] for k in keys],
            idempotency_key=f"helmet-write:{checked['project']}:editor",
            blocked=True,
        )
        editor_show = self.kanban.show(editor_id)
        if sorted(str(p) for p in editor_show.get("parents") or []) != sorted(section_ids.values()):
            raise WritingReviewError("plan_changed", "editor parents differ from the plan")

        # 4. Release only the sections this plan still holds. A section blocked
        #    later by its writer or the coordinator is not ours to release.
        released = []
        for key in keys:
            task_id = section_ids[key]
            if _held_by_plan(self.kanban.show(task_id)):
                self.kanban.unblock(task_id, "plan complete; sources attached")
                released.append(key)
        return {
            "project": checked["project"],
            "sections": section_ids,
            "editor": editor_id,
            "released": released,
        }

    def _require_same_plan(self, project: str, fingerprint: str) -> None:
        """Refuse a plan that differs from the cards already made for the project."""

        for card in self.kanban.list_cards():
            try:
                meta = parse_card_meta(card.get("body"))
            except WritingReviewError:
                continue
            if meta.get("project") == project and meta.get("plan") != fingerprint:
                raise WritingReviewError("plan_changed")

    def _attach_input(self, task_id: str, base: str, data: bytes) -> None:
        sha = ta.sha256_hex(data)
        if any(a.filename == base and a.sha256 == sha for a in self._listing(task_id)):
            return
        ta.upload_artifact_bytes(self.transport, board=self.board, task_id=task_id, name=base, data=data)


@dataclass(frozen=True)
class AcceptedRevision:
    revision: Revision
    artifact: ta.ArtifactRef
    contribution: str
    implementer: str
    reviewer: str
    editorial_decisions: list[str]


def _plan_fingerprint(
    checked: Mapping[str, Any], sources: Mapping[str, Sequence[tuple[str, bytes]]]
) -> str:
    """Identity of a plan: its validated content plus the exact bytes of its inputs."""

    material = {
        "plan": checked,
        "inputs": {
            key: [[name, ta.sha256_hex(data)] for name, data in items]
            for key, items in sorted(sources.items())
        },
    }
    text = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return ta.sha256_hex(text.encode("utf-8"))


def _held_by_plan(show: Mapping[str, Any]) -> bool:
    """True while a card is still in the hold it was created with.

    The newest block/unblock event decides. Native creation records
    ``reason: initial_status``; a block made later by a worker or the
    coordinator carries another reason and is never released here.
    """

    task = show.get("task")
    if not isinstance(task, dict) or task.get("status") != "blocked":
        return False
    events = show.get("events")
    for event in reversed(events if isinstance(events, list) else []):
        if isinstance(event, dict) and event.get("kind") in ("blocked", "unblocked"):
            payload = event.get("payload")
            return (
                event.get("kind") == "blocked"
                and isinstance(payload, dict)
                and payload.get("reason") == "initial_status"
            )
    return False


def _clean_decisions(decisions: Sequence[str]) -> list[str]:
    out = []
    for item in decisions:
        if not isinstance(item, str) or not item.strip() or len(item) > 600:
            raise WritingReviewError("decision_invalid")
        out.append(item.strip())
    return out


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a writing plan (structure only; no content checks)."""

    if not isinstance(plan, Mapping):
        raise WritingReviewError("plan_invalid")
    project = plan.get("project")
    if not isinstance(project, str) or _KEY_RE.fullmatch(project) is None:
        raise WritingReviewError("plan_field_invalid", "project")
    fmt = plan.get("format")
    if fmt not in FORMATS:
        raise WritingReviewError("plan_field_invalid", "format")
    editor = plan.get("editor")
    if not isinstance(editor, Mapping):
        raise WritingReviewError("plan_field_invalid", "editor")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise WritingReviewError("plan_field_invalid", "sections")
    seen: set[str] = set()
    normalized = []
    for raw in sections:
        if not isinstance(raw, Mapping):
            raise WritingReviewError("plan_field_invalid", "sections")
        key = raw.get("key")
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None or key in seen:
            raise WritingReviewError("plan_field_invalid", "section.key")
        seen.add(key)
        normalized.append(
            {
                "key": key,
                "title": _require_text(raw, "title", 200),
                "brief": _require_text(raw, "brief"),
                "assignee": _require_profile(raw.get("assignee"), "section.assignee"),
                "sources": raw.get("sources"),
                "input_files": raw.get("input_files"),
            }
        )
        _bullets(raw.get("sources"), "sources")
    coordinator = _require_profile(plan.get("coordinator"), "coordinator")
    editor_profile = _require_profile(editor.get("assignee"), "editor.assignee")
    writers = {s["assignee"] for s in normalized}
    if coordinator in writers or coordinator == editor_profile:
        raise WritingReviewError("self_review_refused")
    return {
        "project": project,
        "format": fmt,
        "title": _require_text(plan, "title", 200),
        "audience": _require_text(plan, "audience", 500),
        "purpose": _require_text(plan, "purpose", 1000),
        "guidance": _require_text(plan, "guidance"),
        "coordinator": coordinator,
        "editor": {"assignee": editor_profile},
        "sections": normalized,
    }
