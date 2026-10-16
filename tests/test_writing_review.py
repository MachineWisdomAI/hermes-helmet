#!/usr/bin/env python3
"""Revision-bound coordinator review for iterative writing.

Two fixtures prove the contract:

- ``FakeBoard`` models the native Kanban semantics the workflow relies on
  (request-review stages one attachment and moves the card to the reviewer,
  reopen-review restores the writer, complete from review records the caller's
  profile, ``blocked`` is sticky, a running card can complete itself). It is
  driven through the same ``WritingReview`` code as a real board.
- ``NativeBoardTests`` runs the real ``hermes kanban`` CLI in an isolated home
  (never the live board) and is skipped where ``hermes`` is absent.

Synthetic ExampleCo content only.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from hermes_helmet import cli, public_surface  # noqa: E402
from hermes_helmet import task_artifacts as ta  # noqa: E402
from hermes_helmet import writing_context as wc  # noqa: E402
from hermes_helmet import writing_review as wr  # noqa: E402
from hermes_helmet.helmet_issue import HelmetIssueError  # noqa: E402
from test_task_artifacts import FakeTransport  # noqa: E402

BOARD = "wr"
FO = "coordinator"


def _require_yaml(case: unittest.TestCase) -> None:
    """verify.sh provides PyYAML; skip where no YAML loader exists."""
    try:
        import yaml  # noqa: F401
    except ModuleNotFoundError:
        try:
            import ruamel.yaml  # noqa: F401
        except ModuleNotFoundError:
            case.skipTest("no YAML loader installed")


SECTION_MARKER_UNRELATED = "UNRELATED-TASK-MARKER-31c9"
MEMORY_MARKER = "UNRELATED-MEMORY-MARKER-77e2"
SOURCE_FACT = "ExampleCo shipped 12 widgets in March."


class FakeBoard:
    """Native-like in-memory board (see module docstring)."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.tasks: dict[str, dict] = {}
        self.run_seq = 0
        self.task_seq = 0
        self.profile = FO  # the ambient profile of whoever runs the CLI
        self.keys: dict[str, str] = {}
        self.calls: list[str] = []
        self.before_complete = None

    # -- helpers ------------------------------------------------------------
    def _parents_done(self, task: dict) -> bool:
        return all(self.tasks[p]["status"] == "done" for p in task["parents"])

    def _children(self, task_id: str) -> list[str]:
        return [t["id"] for t in self.tasks.values() if task_id in t["parents"]]

    def _promote_children(self, task_id: str) -> None:
        for child in self._children(task_id):
            kid = self.tasks[child]
            if kid["status"] == "todo" and self._parents_done(kid):
                kid["status"] = "ready"

    def _fail(self) -> None:
        raise wr.WritingReviewError("kanban_command_failed")

    # -- KanbanBoard protocol -------------------------------------------------
    def show(self, task_id: str) -> dict:
        self.calls.append("show")
        task = self.tasks[task_id]
        view = {k: task[k] for k in ("id", "title", "body", "assignee", "status")}
        return copy.deepcopy(
            {
                "task": view,
                "parents": list(task["parents"]),
                "children": self._children(task_id),
                "comments": task["comments"],
                "events": [],
                "runs": task["runs"],
            }
        )

    def context(self, task_id: str) -> str:
        task = self.tasks[task_id]
        lines = [f"# Kanban task {task_id}: {task['title']}", "", "## Body", task["body"], ""]
        for row in self.transport.rows.get((BOARD, task_id), []):
            lines.append(f"- attachment {row['filename']}")
        for p in task["parents"]:
            parent = self.tasks[p]
            if parent["status"] == "done" and parent["runs"]:
                last = parent["runs"][-1]
                lines.append(f"parent {p}: {last['summary']} {json.dumps(last['metadata'])}")
        history = [
            (t["id"], r["summary"])
            for t in self.tasks.values()
            if t["id"] != task_id
            for r in t["runs"]
            if r["profile"] == task["assignee"] and r["outcome"] == "completed"
        ]
        if history:
            lines += [f"## Recent work by @{task['assignee']}"]
            lines += [f"- {tid}: {summary}" for tid, summary in history]
            lines.append("")
        lines += ["## Comment thread"]
        lines += [f"{c['author']}: {c['body']}" for c in task["comments"]]
        return "\n".join(lines) + "\n"

    def create(self, title, *, body, assignee, parents=(), idempotency_key=None, blocked=False):  # type: ignore[no-untyped-def]
        if idempotency_key and idempotency_key in self.keys:
            return self.keys[idempotency_key]
        self.task_seq += 1
        task_id = f"t_{self.task_seq:04d}"
        status = "blocked" if blocked else "ready"
        task = {
            "id": task_id, "title": title, "body": body, "assignee": assignee,
            "status": status, "parents": list(parents), "comments": [], "runs": [],
            "implementer": None,
        }
        if not blocked and not self._parents_done(task):
            task["status"] = "todo"
        self.tasks[task_id] = task
        self.transport.task(BOARD, task_id)
        if idempotency_key:
            self.keys[idempotency_key] = task_id
        return task_id

    def claim(self, task_id: str) -> None:
        """Test-only: the dispatcher starts the assignee on a ready card."""
        task = self.tasks[task_id]
        assert task["status"] == "ready", task["status"]
        self.run_seq += 1
        task["status"] = "running"
        task["runs"].append(
            {"id": self.run_seq, "profile": task["assignee"], "status": "running",
             "outcome": None, "summary": None, "metadata": None}
        )

    def request_review(self, task_id: str, name: str, data: bytes, reviewer: str, summary: str) -> ta.ArtifactRef:
        """Test-only: the writer's native request-review (stages the file)."""
        task = self.tasks[task_id]
        assert task["status"] == "running"
        ref = ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id=task_id, name=name, data=data
        )
        run = task["runs"][-1]
        run.update(
            status="review", outcome="review_requested", summary=summary,
            metadata={"artifacts": [f"/worker/attachments/{task_id}/{ref.filename}"]},
        )
        task["implementer"] = task["assignee"]
        task["assignee"] = reviewer
        task["status"] = "review"
        return ref

    def writer_complete(self, task_id: str) -> None:
        """Test-only: the native hole, a running writer completing its own card."""
        task = self.tasks[task_id]
        assert task["status"] == "running"
        run = task["runs"][-1]
        run.update(status="completed", outcome="completed", summary="self", metadata={})
        task["status"] = "done"
        self._promote_children(task_id)

    def reopen_review(self, task_id: str, reason: str) -> None:
        self.calls.append("reopen")
        task = self.tasks[task_id]
        if task["status"] != "review":
            self._fail()
        task["status"] = "ready" if self._parents_done(task) else "todo"
        task["assignee"] = task["implementer"] or task["assignee"]
        task["comments"].append({"author": self.profile, "body": f"CHANGES REQUESTED: {reason}"})

    def complete(self, task_id: str, summary: str, metadata) -> None:  # type: ignore[no-untyped-def]
        self.calls.append("complete")
        if self.before_complete:
            hook, self.before_complete = self.before_complete, None
            hook()
        task = self.tasks[task_id]
        if task["status"] != "review":
            self._fail()
        self.run_seq += 1
        task["runs"].append(
            {"id": self.run_seq, "profile": self.profile, "status": "completed",
             "outcome": "completed", "summary": summary, "metadata": dict(metadata)}
        )
        task["status"] = "done"
        self._promote_children(task_id)

    def block(self, task_id: str, reason: str) -> None:
        task = self.tasks[task_id]
        if task["status"] not in ("ready", "todo", "running"):
            self._fail()
        task["status"] = "blocked"
        task["comments"].append({"author": self.profile, "body": f"BLOCKED: {reason}"})

    def unblock(self, task_id: str, reason: str) -> None:
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            self._fail()
        task["status"] = "ready" if self._parents_done(task) else "todo"

    def edit_body(self, task_id: str, body: str) -> None:
        self.tasks[task_id]["body"] = body

    def comment(self, task_id: str, text: str) -> None:
        self.tasks[task_id]["comments"].append({"author": self.profile, "body": text})

    def link(self, parent_id: str, child_id: str) -> None:
        self.tasks[child_id]["parents"].append(parent_id)

    def unlink(self, parent_id: str, child_id: str) -> None:
        self.tasks[child_id]["parents"].remove(parent_id)


PLAN = {
    "project": "q3-update",
    "format": "document",
    "title": "ExampleCo Q3 Update",
    "audience": "ExampleCo customers",
    "purpose": "Explain what shipped this quarter in plain terms.",
    "guidance": "Plain sentences. Name the product as ExampleCo Widgets.",
    "coordinator": FO,
    "editor": {"assignee": "editor1"},
    "sections": [
        {"key": "shipped", "title": "What shipped", "assignee": "writer1",
         "brief": "Describe what shipped in March.", "sources": [SOURCE_FACT]},
        {"key": "next", "title": "What is next", "assignee": "writer2",
         "brief": "Describe the April plan.", "sources": ["ExampleCo plans 20 widgets in April."]},
    ],
}


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = FakeTransport()
        self.board = FakeBoard(self.transport)
        self.review = wr.WritingReview(BOARD, self.board, self.transport)
        planned = self.review.plan(PLAN)
        self.sections = planned["sections"]
        self.editor = planned["editor"]
        self.s1, self.s2 = self.sections["shipped"], self.sections["next"]

    def submit(self, task: str, text: str, name: str = "section.md", summary: str = "done") -> wr.Submission:
        self.board.claim(task)
        self.board.request_review(task, name, text.encode(), FO, summary)
        return self.review.submission(task)

    def accept(self, task: str, text: str = "v1", decisions=()) -> dict:  # type: ignore[no-untyped-def]
        sub = self.submit(task, text)
        return self.review.accept(task, sub.revision.token, decisions=decisions)


class SubmissionAndFeedbackTests(Harness):
    def test_submission_reaches_coordinator_with_retrievable_native_artifact(self) -> None:
        sub = self.submit(self.s1, "draft one")
        task = self.board.tasks[self.s1]
        self.assertEqual((task["status"], task["assignee"]), ("review", FO))
        self.assertEqual(sub.implementer, "writer1")
        fetched = ta.fetch_artifact(self.transport, sub.artifact, board=BOARD, task_id=self.s1)
        self.assertEqual(fetched.data, b"draft one")
        # The token binds board, task, review run, attachment id and SHA-256.
        parsed = wr.parse_revision(sub.revision.token)
        self.assertEqual(
            (parsed.board, parsed.task_id, parsed.run_id, parsed.attachment_id, parsed.sha256),
            (BOARD, self.s1, sub.revision.run_id, sub.artifact.attachment_id,
             hashlib.sha256(b"draft one").hexdigest()),
        )

    def test_request_changes_requeues_the_correct_card_and_profile(self) -> None:
        sub = self.submit(self.s1, "draft one")
        result = self.review.request_changes(self.s1, sub.revision.token, "Tighten paragraph two.")
        task = self.board.tasks[self.s1]
        self.assertEqual((task["status"], task["assignee"]), ("ready", "writer1"))
        self.assertEqual((result["status"], result["assignee"]), ("ready", "writer1"))
        self.assertIn("CHANGES REQUESTED", task["comments"][-1]["body"])
        self.assertIn(sub.revision.token, task["comments"][-1]["body"])
        # The other contribution is untouched.
        self.assertEqual(self.board.tasks[self.s2]["status"], "ready")
        self.assertEqual(self.board.tasks[self.s2]["assignee"], "writer2")

    def test_revised_card_context_carries_sources_previous_artifact_and_feedback(self) -> None:
        sub = self.submit(self.s1, "draft one")
        self.review.request_changes(self.s1, sub.revision.token, "Tighten paragraph two.")
        context = self.board.context(self.s1)
        self.assertIn(SOURCE_FACT, context)
        self.assertIn(sub.artifact.filename, context)
        self.assertIn("Tighten paragraph two.", context)

    def test_stale_feedback_and_acceptance_are_rejected_and_change_nothing(self) -> None:
        first = self.submit(self.s1, "draft one")
        self.review.request_changes(self.s1, first.revision.token, "Fix it.")
        second = self.submit(self.s1, "draft two", summary="revised")
        self.assertNotEqual(first.revision.token, second.revision.token)
        before = copy.deepcopy(self.board.tasks[self.s1])
        for action in (
            lambda: self.review.request_changes(self.s1, first.revision.token, "late"),
            lambda: self.review.accept(self.s1, first.revision.token),
        ):
            with self.assertRaises(wr.WritingReviewError) as caught:
                action()
            self.assertEqual(caught.exception.code, "stale_revision")
        self.assertEqual(self.board.tasks[self.s1], before)
        # Historical revisions stay readable and distinct.
        history = self.review.history(self.s1)
        self.assertEqual(len(history["revisions"]), 2)
        old = ta.fetch_artifact(self.transport, first.artifact, board=BOARD, task_id=self.s1)
        self.assertEqual(old.data, b"draft one")
        new = ta.fetch_artifact(self.transport, second.artifact, board=BOARD, task_id=self.s1)
        self.assertEqual(new.data, b"draft two")

    def test_acceptance_of_a_card_that_left_review_is_rejected(self) -> None:
        sub = self.submit(self.s1, "draft one")
        self.review.accept(self.s1, sub.revision.token)
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s1, sub.revision.token)
        self.assertEqual(caught.exception.code, "not_in_review")

    def test_revision_of_another_card_or_malformed_token_is_rejected(self) -> None:
        sub1 = self.submit(self.s1, "one")
        self.submit(self.s2, "two")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s2, sub1.revision.token)
        self.assertEqual(caught.exception.code, "revision_task_mismatch")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s1, "not-a-revision")
        self.assertEqual(caught.exception.code, "revision_malformed")

    def test_feedback_is_required_and_bounded(self) -> None:
        sub = self.submit(self.s1, "one")
        for text, code in (("  ", "feedback_required"), ("x" * 9000, "feedback_too_long")):
            with self.assertRaises(wr.WritingReviewError) as caught:
                self.review.request_changes(self.s1, sub.revision.token, text)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.board.tasks[self.s1]["status"], "review")

    def test_escalation_is_reported_after_two_ineffective_iterations(self) -> None:
        for attempt in (1, 2):
            sub = self.submit(self.s1, f"draft {attempt}")
            result = self.review.request_changes(self.s1, sub.revision.token, f"point {attempt}")
        self.assertEqual(result["changes_requested"], 2)
        self.assertTrue(result["escalation_due"])
        first = self.review.history(self.s1)
        self.assertTrue(first["escalation_due"])

    def test_a_race_that_lands_a_different_acceptance_is_detected_and_blocks_release(self) -> None:
        first = self.submit(self.s1, "draft one")

        def other_session() -> None:
            # Another session reopens and the writer resubmits before our call lands.
            self.board.tasks[self.s1]["status"] = "ready"
            self.board.tasks[self.s1]["assignee"] = "writer1"
            self.board.claim(self.s1)
            self.board.request_review(self.s1, "section.md", b"draft two", FO, "revised")

        self.board.before_complete = other_session
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s1, first.revision.token)
        self.assertEqual(caught.exception.code, "acceptance_superseded")
        self.accept(self.s2)
        with self.assertRaises(wr.WritingReviewError):
            self.review.release_editor(self.editor)


class ReviewOwnershipTests(Harness):
    def test_writer_cannot_review_its_own_submission(self) -> None:
        sub = self.submit(self.s1, "draft one")
        for profile_env in ("writer1",):
            with mock.patch.dict(os.environ, {"HERMES_PROFILE": profile_env}):
                with self.assertRaises(wr.WritingReviewError) as caught:
                    self.review.accept(self.s1, sub.revision.token)
                self.assertEqual(caught.exception.code, "self_review_refused")
        self.assertEqual(self.board.tasks[self.s1]["status"], "review")

    def test_submission_routed_to_the_writer_or_a_stranger_is_not_reviewable(self) -> None:
        self.board.claim(self.s1)
        self.board.request_review(self.s1, "section.md", b"x", "writer1", "self-routed")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s1, self.review.submission(self.s1).revision.token)
        self.assertEqual(caught.exception.code, "self_review_refused")
        self.board.tasks[self.s2]["status"] = "ready"
        self.board.claim(self.s2)
        self.board.request_review(self.s2, "section.md", b"y", "somebody-else", "misrouted")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.s2, self.review.submission(self.s2).revision.token)
        self.assertEqual(caught.exception.code, "review_owner_mismatch")

    def test_plan_refuses_a_coordinator_that_is_also_a_writer_or_editor(self) -> None:
        for coordinator in ("writer1", "editor1"):
            with self.assertRaises(wr.WritingReviewError) as caught:
                wr.validate_plan({**PLAN, "coordinator": coordinator})
            self.assertEqual(caught.exception.code, "self_review_refused")

    def test_a_writer_that_completes_its_own_card_never_releases_the_editor(self) -> None:
        # Native Kanban lets a running card complete itself, promoting its children.
        self.board.claim(self.s1)
        self.board.writer_complete(self.s1)
        self.accept(self.s2)
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.release_editor(self.editor)
        self.assertEqual(caught.exception.code, "acceptance_unbound")
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")


class EditorGatingTests(Harness):
    def test_editor_is_held_until_every_section_is_accepted(self) -> None:
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")
        self.assertEqual(sorted(self.board.tasks[self.editor]["parents"]), sorted([self.s1, self.s2]))
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.release_editor(self.editor)
        self.assertEqual(caught.exception.code, "not_accepted")
        self.accept(self.s1)
        # One accepted section: the editor stays blocked even though a parent is done.
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")
        with self.assertRaises(wr.WritingReviewError):
            self.review.release_editor(self.editor)
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")

    def test_editor_receives_exact_accepted_revisions_and_editorial_decisions(self) -> None:
        first = self.submit(self.s1, "draft one")
        self.review.request_changes(self.s1, first.revision.token, "Tighten.")
        second = self.submit(self.s1, "draft two")
        accepted = self.review.accept(
            self.s1, second.revision.token, decisions=["Keep the March figure.", "Cut the history paragraph."]
        )
        self.accept(self.s2, "next v1", decisions=["Lead with the April date."])
        result = self.review.release_editor(self.editor)
        self.assertEqual(result["status"], "ready")
        files = {r["filename"]: r for r in self.transport.rows[(BOARD, self.editor)]}
        by_contribution = {i["contribution"]: i for i in result["inputs"]}
        shipped = by_contribution["shipped"]
        self.assertEqual(shipped["revision"], accepted["accepted_revision"])
        self.assertEqual(
            self.transport.blobs[files[shipped["input_file"]]["id"]], b"draft two"
        )
        # The superseded first draft never enters the editor card.
        self.assertNotIn(b"draft one", [self.transport.blobs[r["id"]] for r in files.values()])
        body = self.board.tasks[self.editor]["body"]
        self.assertIn("Cut the history paragraph.", body)
        self.assertIn("Lead with the April date.", body)
        self.assertIn(accepted["accepted_revision"], body)
        context = self.board.context(self.editor)
        for expected in (accepted["accepted_revision"], "Keep the March figure."):
            self.assertIn(expected, context)

    def test_editor_alone_assembles_and_the_canonical_artifact_is_its_accepted_revision(self) -> None:
        self.accept(self.s1)
        self.accept(self.s2)
        self.review.release_editor(self.editor)
        self.board.claim(self.editor)
        self.board.request_review(self.editor, "final.md", b"assembled", FO, "assembled")
        sub = self.review.submission(self.editor)
        self.review.accept(self.editor, sub.revision.token)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "final.md"
            canonical = self.review.canonical(self.editor, str(out))
            self.assertEqual(out.read_bytes(), b"assembled")
        self.assertEqual(canonical["revision"], sub.revision.token)
        # Sections cannot be asked for the canonical document.
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.canonical(self.s1)
        self.assertEqual(caught.exception.code, "wrong_card_role")

    def test_later_uploads_never_replace_the_accepted_revision(self) -> None:
        accepted = self.accept(self.s1, "accepted text")
        # A concurrent or later worker attaches another file under the same name.
        ta.upload_artifact_bytes(
            self.transport, board=BOARD, task_id=self.s1, name="section.md", data=b"overwrite attempt"
        )
        again = self.review.accepted_revision(self.s1)
        self.assertEqual(again.revision.token, accepted["accepted_revision"])
        fetched = ta.fetch_artifact(self.transport, again.artifact, board=BOARD, task_id=self.s1)
        self.assertEqual(fetched.data, b"accepted text")
        self.assertEqual(len(ta.list_artifacts(self.transport, board=BOARD, task_id=self.s1)), 2)

    def test_two_writers_submit_separately_without_touching_each_other(self) -> None:
        a = self.submit(self.s1, "alpha")
        b = self.submit(self.s2, "beta")
        self.assertNotEqual((a.revision.task_id), (b.revision.task_id))
        self.assertEqual(
            [r.filename for r in ta.list_artifacts(self.transport, board=BOARD, task_id=self.s1)],
            ["section.md"],
        )
        self.assertEqual(
            ta.fetch_artifact(self.transport, b.artifact).data, b"beta"
        )
        self.assertEqual(self.transport.rows[(BOARD, self.editor)], [])


class SupersedeTests(Harness):
    def test_superseding_an_accepted_section_regates_the_editor_and_invalidates_inputs(self) -> None:
        self.accept(self.s1, "v1")
        self.accept(self.s2, "v1")
        self.review.release_editor(self.editor)
        self.assertIn("Accepted revision", self.board.tasks[self.editor]["body"])
        result = self.review.supersede_section(self.editor, self.s1, reason="Source changed.")
        self.assertEqual(result["editor_status"], "blocked")
        editor_body = self.board.tasks[self.editor]["body"]
        self.assertNotIn("Accepted revision:", editor_body)
        self.assertIn("none yet", editor_body)
        new_id = result["replacement"]
        self.assertEqual(self.board.tasks[new_id]["assignee"], "writer1")
        self.assertIn("Supersedes", self.board.tasks[new_id]["body"])
        self.assertIn(SOURCE_FACT, self.board.tasks[new_id]["body"])
        self.assertIn("SUPERSEDED", self.board.tasks[self.s1]["comments"][-1]["body"])
        self.assertEqual(sorted(self.board.tasks[self.editor]["parents"]), sorted([new_id, self.s2]))
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.release_editor(self.editor)
        self.assertEqual(caught.exception.code, "not_accepted")
        # Accept the replacement and release again with the new exact revision.
        self.board.tasks[new_id]["status"] = "ready"
        accepted = self.accept(new_id, "v2")
        released = self.review.release_editor(self.editor)
        shipped = next(i for i in released["inputs"] if i["contribution"] == "shipped")
        self.assertEqual(shipped["revision"], accepted["accepted_revision"])
        self.assertEqual(shipped["task_id"], new_id)

    def test_editor_submission_is_not_acceptable_after_an_input_changes(self) -> None:
        self.accept(self.s1)
        self.accept(self.s2)
        self.review.release_editor(self.editor)
        self.board.claim(self.editor)
        self.board.request_review(self.editor, "final.md", b"assembled", FO, "assembled")
        sub = self.review.submission(self.editor)
        # Reopen path: the editor in review is returned and re-gated, so the
        # old submission cannot be accepted any more.
        self.review.supersede_section(self.editor, self.s1, reason="Source changed.")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.editor, sub.revision.token)
        self.assertIn(caught.exception.code, {"not_in_review", "stale_revision"})
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")

    def test_editor_acceptance_requires_inputs_to_still_be_the_accepted_ones(self) -> None:
        self.accept(self.s1)
        self.accept(self.s2)
        self.review.release_editor(self.editor)
        self.board.claim(self.editor)
        self.board.request_review(self.editor, "final.md", b"assembled", FO, "assembled")
        sub = self.review.submission(self.editor)
        # Tamper: the recorded inputs no longer match the sections' accepted state.
        body = self.board.tasks[self.editor]["body"].replace("-r", "-x")
        data = wr.recorded_inputs(body)
        data[0]["revision"] = "helmet-write-revision:v1:wr:t_x:1:1:" + "0" * 64
        self.board.tasks[self.editor]["body"] = wr._replace_inputs(body, data)
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(self.editor, sub.revision.token)
        self.assertEqual(caught.exception.code, "editor_inputs_stale")
        self.assertEqual(self.board.tasks[self.editor]["status"], "review")

    def test_a_running_editor_cannot_be_regated_in_place(self) -> None:
        self.accept(self.s1)
        self.accept(self.s2)
        self.review.release_editor(self.editor)
        self.board.claim(self.editor)
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.supersede_section(self.editor, self.s1, reason="x")
        self.assertEqual(caught.exception.code, "editor_running")


class PlanAndCardTests(Harness):
    def test_plan_creates_one_card_per_contribution_plus_a_blocked_editor(self) -> None:
        self.assertEqual(len(self.board.tasks), 3)
        self.assertEqual(self.board.tasks[self.editor]["status"], "blocked")
        self.assertEqual(self.board.tasks[self.s1]["assignee"], "writer1")
        self.assertEqual(self.board.tasks[self.s2]["assignee"], "writer2")
        again = self.review.plan(PLAN)
        self.assertEqual(again["sections"], self.sections)
        self.assertEqual(again["editor"], self.editor)
        self.assertEqual(len(self.board.tasks), 3)

    def test_document_workers_get_writing_guidance_not_captain_authority(self) -> None:
        for task_id in (self.s1, self.s2, self.editor):
            body = self.board.tasks[task_id]["body"]
            for forbidden in ("Captain", "helmet-issue", "helmet-epic", "helmet write", "merge", "authority", "policy"):
                self.assertNotIn(forbidden, body, (task_id, forbidden))
            self.assertIn("Audience: ExampleCo customers", body)
            self.assertIn("Plain sentences.", body)
            self.assertIn("kanban_request_review", body)
        self.assertIn(SOURCE_FACT, self.board.tasks[self.s1]["body"])
        editor_body = self.board.tasks[self.editor]["body"]
        for topic in ("connected passages", "factual claim", "intentional cut", "before compressing"):
            self.assertIn(topic, editor_body.replace("\n", " ").replace("  ", " "))
        self.assertIn("only assembler", editor_body)
        section_body = self.board.tasks[self.s1]["body"]
        self.assertIn("never edit another card or the final document", section_body.replace("\n", " "))

    def test_plan_input_files_are_attached_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "brief.md").write_text("source brief")
            plan = copy.deepcopy(PLAN)
            plan["project"] = "with-input"
            plan["sections"][0]["input_files"] = ["brief.md"]
            first = self.review.plan(plan, base_dir=tmp)
            self.review.plan(plan, base_dir=tmp)
        rows = self.transport.rows[(BOARD, first["sections"]["shipped"])]
        self.assertEqual([r["filename"] for r in rows], ["brief.md"])

    def test_bad_plans_fail_with_fixed_codes(self) -> None:
        cases = [
            ({**PLAN, "format": "pdf"}, "plan_field_invalid"),
            ({**PLAN, "project": "Bad Key"}, "plan_field_invalid"),
            ({**PLAN, "sections": []}, "plan_field_invalid"),
            ({**PLAN, "sections": [PLAN["sections"][0], PLAN["sections"][0]]}, "plan_field_invalid"),
            ({**PLAN, "audience": " "}, "plan_field_invalid"),
            ("nope", "plan_invalid"),
        ]
        for plan, code in cases:
            with self.assertRaises(wr.WritingReviewError) as caught:
                wr.validate_plan(plan)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, code)

    def test_deck_format_is_supported_for_slide_assignments(self) -> None:
        deck = {**PLAN, "project": "deck", "format": "deck"}
        planned = self.review.plan(deck)
        self.assertIn("Format: deck", self.board.tasks[planned["editor"]]["body"])

    def test_errors_never_echo_card_text(self) -> None:
        sub = self.submit(self.s1, "secret-body-sentinel")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.request_changes(self.s1, sub.revision.token.replace("t_", "t_x"), "f")
        self.assertNotIn("secret-body-sentinel", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith("err:"))


class ContextIsolationTests(unittest.TestCase):
    def native_like_context(self, extra: str = "") -> str:
        return (
            "# Kanban task t_1: Contribution\n\n## Body\n"
            f"{SOURCE_FACT}\n\n## Prior attempts on this task\nattempt one\n\n"
            "## Recent work by @writer1\n"
            f"- t_9 — Old job ({SECTION_MARKER_UNRELATED}): done\n\n"
            "## Comment thread\ncomment from worker `coordinator`: CHANGES REQUESTED: tighten\n" + extra
        )

    def result(self, body: str, context: str) -> str:
        return json.dumps({"task": {"id": "t_1", "body": body}, "worker_context": context})

    def test_role_history_is_removed_only_for_writing_cards_and_facts_are_retained(self) -> None:
        context = self.native_like_context()
        out = wc.transform_tool_result(
            tool_name="kanban_show", result=self.result(f"{wc.CARD_MARKER}\nbody", context)
        )
        assert out is not None
        cleaned = json.loads(out)["worker_context"]
        self.assertNotIn(SECTION_MARKER_UNRELATED, cleaned)
        self.assertNotIn("Recent work by", cleaned)
        for kept in (SOURCE_FACT, "Prior attempts on this task", "CHANGES REQUESTED: tighten"):
            self.assertIn(kept, cleaned)
        # Other cards and other tools are untouched.
        self.assertIsNone(wc.transform_tool_result(tool_name="kanban_show", result=self.result("plain", context)))
        self.assertIsNone(wc.transform_tool_result(tool_name="terminal", result=self.result(wc.CARD_MARKER, context)))
        self.assertIsNone(wc.transform_tool_result(tool_name="kanban_show", result="not json"))
        self.assertIsNone(
            wc.transform_tool_result(
                tool_name="kanban_show",
                result=self.result(wc.CARD_MARKER, "# nothing to strip\n"),
            )
        )

    def test_history_section_at_the_end_of_the_context_is_removed_cleanly(self) -> None:
        text = "## Body\nfact\n\n## Recent work by @w\n- t_9: old\n"
        self.assertEqual(wc.strip_role_history(text), "## Body\nfact\n")

    def test_plugin_registers_the_transform_hook_and_writes_a_loadable_directory(self) -> None:
        ctx = mock.Mock()
        wc.register(ctx)
        ctx.register_hook.assert_called_once_with("transform_tool_result", wc.transform_tool_result)
        with tempfile.TemporaryDirectory() as tmp:
            path = wc.write_plugin(tmp)
            self.assertEqual(sorted(p.name for p in path.iterdir()), ["__init__.py", "plugin.yaml"])
            namespace: dict = {}
            exec(compile((path / "__init__.py").read_text(), "plugin", "exec"), namespace)  # noqa: S102
            self.assertIs(namespace["register"], wc.register)
            self.assertIn("transform_tool_result", (path / "plugin.yaml").read_text())

    def test_profile_check_reports_memory_and_plugin_gaps_explicitly(self) -> None:
        loose = {"memory": {"memory_enabled": True, "user_profile_enabled": True, "provider": "openviking"}}
        codes = {f["code"] for f in wc.profile_findings(loose)}
        self.assertEqual(
            codes,
            {"builtin_memory_enabled", "user_profile_enabled", "external_memory_provider", "context_plugin_not_enabled"},
        )
        defaults_loaded = {f["code"] for f in wc.profile_findings({})}
        self.assertIn("builtin_memory_enabled", defaults_loaded)
        ready = {
            "memory": {"memory_enabled": False, "user_profile_enabled": False, "provider": ""},
            "plugins": {"enabled": [wc.PLUGIN_NAME]},
        }
        self.assertEqual(wc.profile_findings(ready), [])
        denied = {**ready, "plugins": {"enabled": [wc.PLUGIN_NAME], "disabled": [wc.PLUGIN_NAME]}}
        self.assertEqual([f["code"] for f in wc.profile_findings(denied)], ["context_plugin_not_enabled"])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_every_write_verb_is_registered(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["write", "plan", "b", "plan.json"],
            ["write", "submission", "b", "t"],
            ["write", "history", "b", "t"],
            ["write", "request-changes", "b", "t", "--revision", "r", "--feedback", "f"],
            ["write", "accept", "b", "t", "--revision", "r", "--decision", "d"],
            ["write", "release-editor", "b", "t"],
            ["write", "supersede", "b", "t", "--editor", "e", "--reason", "r"],
            ["write", "canonical", "b", "t"],
            ["write", "context", "b", "t"],
            ["write", "context-plugin", "dir"],
            ["write", "profile-check", "config.yaml"],
        ):
            self.assertIs(parser.parse_args(argv).func, cli.cmd_write, argv)

    def test_context_plugin_command_writes_and_names_the_enable_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _err = self.run_cli("write", "context-plugin", tmp)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue((Path(payload["path"]) / "plugin.yaml").is_file())
            self.assertEqual(payload["next"], f"hermes plugins enable {wc.PLUGIN_NAME}")

    def test_bad_revision_reports_a_fixed_code_not_a_traceback(self) -> None:
        board = mock.Mock()
        with mock.patch.object(wr, "HermesKanban", return_value=board), mock.patch.object(
            cli, "_artifact_transport", return_value=mock.Mock()
        ):
            code, out, err = self.run_cli(
                "write", "accept", BOARD, "t_1", "--revision", "nope", "--worker-runtime", "x"
            )
        self.assertEqual(code, 1)
        self.assertIn("err:revision_malformed", err)
        self.assertEqual(out, "")

    def test_profile_check_command(self) -> None:
        _require_yaml(self)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("memory:\n  memory_enabled: true\n")
            code, out, _err = self.run_cli("write", "profile-check", str(config))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["fresh_context_ready"])


# --------------------------------------------------------------------------
# Real `hermes kanban` in an isolated home.
# --------------------------------------------------------------------------

HERMES = shutil.which("hermes")


class CleanEnvRunner:
    """Runs ``hermes`` against a scratch home; never inherits the live board."""

    def __init__(self, home: Path, profile: str = FO) -> None:
        self.home = home
        self.profile = profile

    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home / "user-home"),
            "HERMES_HOME": str(self.home),
            "HERMES_KANBAN_DB": str(self.home / "kanban.db"),
            "HERMES_KANBAN_BOARD": BOARD,
            "HERMES_PROFILE": self.profile,
            "TMPDIR": str(self.home),
        }

    def run(self, command):  # type: ignore[no-untyped-def]
        done = subprocess.run(
            list(command), env=self.env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if done.returncode:
            raise HelmetIssueError(f"err:command_failed:exit{done.returncode}")
        return done.stdout


class NativeAttachmentTransport:
    """Test adapter over native attachments (what a deployment adapter wraps)."""

    def __init__(self, runner: CleanEnvRunner) -> None:
        self.runner = runner

    def _rows(self, task_id: str) -> list[dict]:
        out = self.runner.run(["hermes", "kanban", "--board", BOARD, "attachments", task_id, "--json"])
        return json.loads(out)

    def list_attachments(self, board, task_id):  # type: ignore[no-untyped-def]
        rows = []
        for row in self._rows(task_id):
            data = Path(row["stored_path"]).read_bytes()
            rows.append({**row, "task_id": task_id, "sha256": hashlib.sha256(data).hexdigest()})
        return rows

    def download(self, board, task_id, attachment_id):  # type: ignore[no-untyped-def]
        row = next(r for r in self._rows(task_id) if r["id"] == attachment_id)
        return Path(row["stored_path"]).read_bytes()

    def upload(self, board, task_id, *, name, media_type, data, sha256):  # type: ignore[no-untyped-def]
        staging = self.runner.home / "upload-staging"
        staging.mkdir(exist_ok=True)
        source = staging / name
        source.write_bytes(data)
        self.runner.run(
            ["hermes", "kanban", "--board", BOARD, "attach", task_id, str(source),
             "--name", name, "--content-type", media_type]
        )
        return next(r for r in reversed(self.list_attachments(board, task_id)) if r["filename"] == name)


@unittest.skipUnless(HERMES, "hermes CLI unavailable")
class NativeBoardTests(unittest.TestCase):
    """Proves the seam assumptions against the real Kanban CLI and context builder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "user-home").mkdir()
        self.runner = CleanEnvRunner(self.home)
        self.run_cmd("hermes", "kanban", "boards", "create", BOARD)
        self.run_cmd("hermes", "kanban", "--board", BOARD, "init")
        self.transport = NativeAttachmentTransport(self.runner)
        self.board = wr.HermesKanban(BOARD, self.runner)
        self.review = wr.WritingReview(BOARD, self.board, self.transport)

    def run_cmd(self, *argv: str, profile: str | None = None) -> str:
        runner = CleanEnvRunner(self.home, profile or FO)
        return runner.run(list(argv))

    def kanban(self, *argv: str, profile: str | None = None) -> str:
        return self.run_cmd("hermes", "kanban", "--board", BOARD, *argv, profile=profile)

    def workspace(self, task_id: str) -> Path:
        path = self.home / "kanban" / "boards" / BOARD / "workspaces" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def submit(self, task_id: str, filename: str, text: str, summary: str = "done") -> wr.Submission:
        self.kanban("claim", task_id)
        path = self.workspace(task_id) / filename
        path.write_text(text)
        self.kanban(
            "request-review", task_id, "--reviewer", FO, "--summary", summary,
            "--metadata", json.dumps({"artifacts": [str(path)]}), profile="writer1",
        )
        return self.review.submission(task_id)

    def plan(self) -> dict:
        plan = copy.deepcopy(PLAN)
        plan["sections"] = [dict(plan["sections"][0], assignee="writer1"),
                            dict(plan["sections"][1], assignee="writer1")]
        return self.review.plan(plan)

    def test_end_to_end_review_loop_gates_the_editor_on_accepted_revisions(self) -> None:
        planned = self.plan()
        s1, s2, editor = planned["sections"]["shipped"], planned["sections"]["next"], planned["editor"]
        self.assertEqual(json.loads(self.kanban("show", editor, "--json"))["task"]["status"], "blocked")

        first = self.submit(s1, "section.md", "draft one")
        self.assertEqual(first.reviewer, FO)
        self.assertEqual(first.implementer, "writer1")
        self.assertEqual(self.transport.download(BOARD, s1, first.artifact.attachment_id), b"draft one")

        # request-changes: real native dispatch back to the writing profile.
        result = self.review.request_changes(s1, first.revision.token, "Tighten paragraph two.")
        self.assertEqual((result["status"], result["assignee"]), ("ready", "writer1"))
        shown = json.loads(self.kanban("show", s1, "--json"))
        self.assertEqual(shown["task"]["status"], "ready")
        context = self.kanban("context", s1)
        self.assertIn("Tighten paragraph two.", context)
        self.assertIn(SOURCE_FACT, context)
        self.assertIn(first.artifact.filename, context)

        second = self.submit(s1, "section.md", "draft two", summary="revised")
        self.assertNotEqual(first.revision.token, second.revision.token)
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.accept(s1, first.revision.token)
        self.assertEqual(caught.exception.code, "stale_revision")
        history = self.review.history(s1)
        self.assertEqual([r["revision"] for r in history["revisions"]],
                         [first.revision.token, second.revision.token])
        self.assertEqual(self.transport.download(BOARD, s1, first.artifact.attachment_id), b"draft one")

        accepted = self.review.accept(s1, second.revision.token, decisions=["Keep the March figure."])
        self.assertEqual(self.review.accepted_revision(s1).revision.token, accepted["accepted_revision"])
        # One accepted section: editor still blocked natively, and release refuses.
        self.assertEqual(json.loads(self.kanban("show", editor, "--json"))["task"]["status"], "blocked")
        with self.assertRaises(wr.WritingReviewError):
            self.review.release_editor(editor)

        self.submit(s2, "section.md", "next draft")
        self.review.accept(s2, self.review.submission(s2).revision.token)
        released = self.review.release_editor(editor)
        self.assertEqual(released["status"], "ready")
        editor_context = self.kanban("context", editor)
        self.assertIn(accepted["accepted_revision"], editor_context)
        self.assertIn("Keep the March figure.", editor_context)
        names = [r["filename"] for r in self.transport.list_attachments(BOARD, editor)]
        self.assertEqual(len(names), 2)
        self.assertNotIn("draft one", "".join(
            self.transport.download(BOARD, editor, r["id"]).decode() for r in self.transport._rows(editor)
        ))

    def test_native_self_completion_hole_is_closed_by_the_release_check(self) -> None:
        planned = self.plan()
        s1, s2, editor = planned["sections"]["shipped"], planned["sections"]["next"], planned["editor"]
        self.kanban("claim", s1)
        # The writer completes its own running card: native allows this.
        self.kanban("complete", s1, "--summary", "self done", profile="writer1")
        self.submit(s2, "section.md", "next draft")
        self.review.accept(s2, self.review.submission(s2).revision.token)
        self.assertEqual(json.loads(self.kanban("show", editor, "--json"))["task"]["status"], "blocked")
        with self.assertRaises(wr.WritingReviewError) as caught:
            self.review.release_editor(editor)
        self.assertEqual(caught.exception.code, "acceptance_unbound")

    def test_writer_cannot_approve_after_requesting_review(self) -> None:
        planned = self.plan()
        s1 = planned["sections"]["shipped"]
        sub = self.submit(s1, "section.md", "draft")
        with self.assertRaises(wr.WritingReviewError) as caught:
            with mock.patch.dict(os.environ, {"HERMES_PROFILE": "writer1"}):
                self.review.accept(s1, sub.revision.token)
        self.assertEqual(caught.exception.code, "self_review_refused")
        self.assertEqual(json.loads(self.kanban("show", s1, "--json"))["task"]["status"], "review")

    def test_fixture_with_unrelated_task_and_memory_markers_shows_exclusion_and_retention(self) -> None:
        planned = self.plan()
        s1 = planned["sections"]["shipped"]
        # An unrelated completed task for the same assignee carries a marker.
        other = json.loads(
            self.kanban("create", f"Other job {SECTION_MARKER_UNRELATED}", "--assignee", "writer1", "--json")
        )["id"]
        self.kanban("claim", other)
        self.kanban("complete", other, "--summary", f"finished {SECTION_MARKER_UNRELATED}", profile="writer1")
        # Native context for the writing card leaks it: this is the gap.
        native = self.kanban("context", s1)
        self.assertIn("Recent work by @writer1", native)
        self.assertIn(SECTION_MARKER_UNRELATED, native)
        # The supported plugin seam removes it, keeping the assigned facts.
        tool_result = json.dumps(
            {"task": json.loads(self.kanban("show", s1, "--json"))["task"], "worker_context": native}
        )
        cleaned = json.loads(wc.transform_tool_result(tool_name="kanban_show", result=tool_result))["worker_context"]
        self.assertNotIn(SECTION_MARKER_UNRELATED, cleaned)
        self.assertNotIn("Recent work by", cleaned)
        self.assertIn(SOURCE_FACT, cleaned)
        self.assertIn("kanban_request_review", cleaned)
        # An unmarked card keeps native behaviour.
        plain = json.loads(self.kanban("create", "Plain", "--assignee", "writer1", "--json"))["id"]
        plain_result = json.dumps(
            {"task": json.loads(self.kanban("show", plain, "--json"))["task"],
             "worker_context": self.kanban("context", plain)}
        )
        self.assertIsNone(wc.transform_tool_result(tool_name="kanban_show", result=plain_result))

    def test_memory_markers_in_the_effective_prompt_follow_profile_config(self) -> None:
        memories = self.home / "memories"
        memories.mkdir(exist_ok=True)
        (memories / "MEMORY.md").write_text(MEMORY_MARKER)

        def memory_chars() -> int:
            out = self.run_cmd("hermes", "prompt-size", "--json")
            return int(json.loads(out)["memory"]["chars"])

        self.assertGreater(memory_chars(), 0)
        (self.home / "config.yaml").write_text(
            "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n  provider: ''\n"
            f"plugins:\n  enabled:\n  - {wc.PLUGIN_NAME}\n"
        )
        self.assertEqual(memory_chars(), 0)
        _require_yaml(self)
        from hermes_helmet.worker_role_runtime import _yaml_load

        config = _yaml_load((self.home / "config.yaml").read_text())
        self.assertEqual(wc.profile_findings(config), [])


class PackagingTests(unittest.TestCase):
    def test_skill_and_references_are_packaged_and_installable(self) -> None:
        skill = ROOT / "skills" / "helmet-write"
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertGreaterEqual(len(list((skill / "references").glob("*.md"))), 3)
        from hermes_helmet.install_skills import install_skills

        with tempfile.TemporaryDirectory() as tmp:
            installed = install_skills(targets=["codex", "claude", "hermes"], prefix=Path(tmp))
            self.assertTrue(any("helmet-write" in str(item) for item in installed))
            for target in Path(tmp).rglob("helmet-write"):
                self.assertTrue((target / "SKILL.md").is_file())
                self.assertTrue((target / "references").is_dir())

    def test_skill_is_captain_reserved_so_workers_never_import_it(self) -> None:
        from hermes_helmet.company_skills import CAPTAIN_RESERVED_SKILL_NAMES

        self.assertIn("helmet-write", CAPTAIN_RESERVED_SKILL_NAMES)

    def test_core_skill_is_concise_and_covers_the_required_topics(self) -> None:
        text = (ROOT / "skills" / "helmet-write" / "SKILL.md").read_text()
        self.assertLess(len(text.splitlines()), 120)
        lowered = text.lower()
        for topic in (
            "short edit", "review-only", "one card per", "request-changes", "revision",
            "two", "editor", "reference",
        ):
            self.assertIn(topic, lowered, topic)
        refs = "".join(p.read_text().lower() for p in (ROOT / "skills" / "helmet-write" / "references").glob("*.md"))
        for topic in (
            "slide", "connected", "factual", "intentional cut", "before length compression", "reassign",
            "mcp", "optional",
        ):
            self.assertIn(topic, refs, topic)

    def test_new_files_stay_inside_the_public_surface(self) -> None:
        self.assertEqual(public_surface.scan_public_surface(ROOT), [])


if __name__ == "__main__":
    unittest.main()
