#!/usr/bin/env python3
"""Review-only repositories join native requested-review intake only.

A repository accepted through ``prepare-repo --purpose review`` is admitted to
requested-review discovery and managed review execution, but never to issue
implementation, repair dispatch, or implementation crew authority.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import authority  # noqa: E402
from hermes_helmet import github_issue_poller as poller  # noqa: E402
from hermes_helmet import install_poller as installer  # noqa: E402
from hermes_helmet import repo_enroll as re_mod  # noqa: E402


FIXTURE_POLICY = ROOT / "config" / "fixtures" / "exampleco" / "policy.json"
CAPTAIN = "example-captain"
WORKER = "example-agent"
WORK_SLUG = "example-org/demo-repo"
REVIEW_SLUG = "example-org/review-repo"
OTHER_SLUG = "example-org/unenrolled-repo"
HEAD = "b" * 40


def mapping(root: Path, **overrides: object) -> dict[str, object]:
    raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
    work = root / "demo-repo"
    work.mkdir(exist_ok=True)
    raw["repositories"] = [{"slug": WORK_SLUG, "worktree": str(work)}]
    raw.pop("issue_routes", None)
    raw.update(overrides)
    return raw


def review_entry(root: Path, slug: str = REVIEW_SLUG) -> dict[str, str]:
    checkout = root / slug.rsplit("/", 1)[1]
    checkout.mkdir(exist_ok=True)
    return {"slug": slug, "worktree": str(checkout)}


def make_policy(root: Path, **overrides: object) -> authority.Policy:
    return authority.policy_from_mapping(mapping(root, **overrides))


def pull(number: int, *, reviewer: str = WORKER) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "head": {"sha": HEAD},
        "requested_reviewers": [{"login": reviewer}],
    }


def request_event(event_id: int) -> dict[str, object]:
    return {
        "id": event_id,
        "event": "review_requested",
        "requested_reviewer": {"login": WORKER},
    }


class Runner:
    """Fake gh/git/hermes covering several repositories."""

    def __init__(
        self,
        pulls: dict[str, list[object]],
        events: dict[tuple[str, int], list[object]],
        issues: dict[str, list[object]] | None = None,
    ) -> None:
        self.pulls = pulls
        self.events = events
        self.issues = issues or {}
        self.creates: list[list[str]] = []
        self.tasks: dict[str, str] = {}
        self.endpoints: list[str] = []

    def run(self, command: list[str]) -> str:
        if command[:3] == [poller.GH, "api", "user"]:
            return f"{WORKER}\n"
        if command[:3] == [poller.GH, "api", "--paginate"]:
            endpoint = command[-1]
            self.endpoints.append(endpoint)
            path = endpoint.split("?", 1)[0]
            parts = path.split("/")  # repos/OWNER/NAME/<kind>[/N/events]
            slug = "/".join(parts[1:3])
            if parts[3] == "pulls":
                return json.dumps(self.pulls.get(slug, []))
            if parts[3] == "issues" and len(parts) == 4:
                return json.dumps(self.issues.get(slug, []))
            if parts[3] == "issues" and parts[5] == "events":
                return json.dumps(self.events.get((slug, int(parts[4])), []))
            raise AssertionError(command)
        if command[0] == "git":
            return "true\n"
        if command[:2] == [poller.HERMES, "kanban"] and "list" in command:
            listed: list[dict[str, object]] = []
            for create in self.creates:
                key = create[create.index("--idempotency-key") + 1]
                if any(item["id"] == self.tasks[key] for item in listed):
                    continue
                listed.append(
                    {
                        "id": self.tasks[key],
                        "created_by": poller.CREATED_BY,
                        "branch_name": create[create.index("--branch") + 1],
                        "status": "ready",
                    }
                )
            return json.dumps(listed)
        if command[:2] == [poller.HERMES, "kanban"] and "create" in command:
            self.creates.append(command)
            key = command[command.index("--idempotency-key") + 1]
            task_id = self.tasks.setdefault(key, f"t_review_{len(self.tasks) + 1}")
            return json.dumps({"id": task_id})
        raise AssertionError(command)

    def repos_queried(self) -> set[str]:
        return {"/".join(e.split("?", 1)[0].split("/")[1:3]) for e in self.endpoints}


class ReviewOnlyIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def policy(self, **overrides: object) -> authority.Policy:
        overrides.setdefault("review_repositories", [review_entry(self.root)])
        return make_policy(self.root, **overrides)

    def runner(self, **kwargs: object) -> Runner:
        return Runner(
            pulls={REVIEW_SLUG: [pull(7)], WORK_SLUG: [pull(3)], OTHER_SLUG: [pull(9)]},
            events={
                (REVIEW_SLUG, 7): [request_event(11)],
                (WORK_SLUG, 3): [request_event(21)],
                (OTHER_SLUG, 9): [request_event(31)],
            },
            **kwargs,
        )

    # -- intake -----------------------------------------------------------

    def test_pending_request_in_review_only_repository_creates_one_managed_task(self) -> None:
        policy = self.policy()
        runner = self.runner()
        result = poller.run_once(policy, self.ledger, runner)
        self.assertEqual(result.errors, [])
        keys = {
            c[c.index("--idempotency-key") + 1]: c for c in runner.creates
        }
        review_key = f"github-pr-review-request:{REVIEW_SLUG}:7:11"
        self.assertEqual(
            set(keys),
            {review_key, f"github-pr-review-request:{WORK_SLUG}:3:21"},
        )
        create = keys[review_key]
        # The ordinary review task: same identity, branch, role, no PR contract.
        self.assertEqual(create[create.index("--assignee") + 1], policy.assignee)
        self.assertEqual(create[create.index("--created-by") + 1], poller.CREATED_BY)
        self.assertIn("review/review-repo-pr-7-11", create)
        self.assertNotIn("--completion-contract", create)
        self.assertEqual(
            create[create.index("--workspace") + 1],
            f"worktree:{self.root / 'review-repo'}",
        )
        body = create[create.index("--body") + 1]
        self.assertIn(f"Repository scope: review-only ({REVIEW_SLUG})", body)
        self.assertIn(
            f"Helmet-Review-Request: https://github.com/{REVIEW_SLUG}/pull/7#event-11", body
        )
        self.assertIn("reading and reviewing that pull request only", body)
        scopes = {r.repository.slug: r.scope for r, _ in result.review_tasks}
        self.assertEqual(
            scopes, {REVIEW_SLUG: "review", WORK_SLUG: "work"}
        )

    def test_repeated_cycle_reuses_task_and_renewal_is_distinct(self) -> None:
        policy = self.policy()
        runner = self.runner()
        first = poller.run_once(policy, self.ledger, runner)
        second = poller.run_once(policy, self.ledger, runner)
        self.assertEqual(
            {t for _, t in first.review_tasks}, {t for _, t in second.review_tasks}
        )
        self.assertEqual(len(runner.tasks), 2)
        # A push keeps the same request id: still no new task.
        runner.pulls[REVIEW_SLUG] = [{**pull(7), "head": {"sha": "c" * 40}}]
        poller.run_once(policy, self.ledger, runner)
        self.assertEqual(len(runner.tasks), 2)
        # A renewed request is a distinct task, behind the active attempt.
        runner.events[(REVIEW_SLUG, 7)].append(request_event(40))
        poller.run_once(policy, self.ledger, runner)
        self.assertEqual(len(runner.tasks), 3)
        renewed = runner.creates[-1]
        self.assertIn("review/review-repo-pr-7-40", renewed)
        self.assertEqual(
            [renewed[i + 1] for i, a in enumerate(renewed) if a == "--parent"],
            [runner.tasks[f"github-pr-review-request:{REVIEW_SLUG}:7:11"]],
        )

    def test_unenrolled_repository_is_never_queried_for_either_intake(self) -> None:
        policy = self.policy()
        runner = self.runner()
        poller.run_once(policy, self.ledger, runner)
        self.assertNotIn(OTHER_SLUG, runner.repos_queried())
        self.assertFalse(any(OTHER_SLUG in key for key in runner.tasks))

    # -- denial of implementation authority -------------------------------

    def test_review_only_repository_is_denied_issue_implementation(self) -> None:
        policy = self.policy()
        eligible = {
            "number": 5,
            "title": "Implement something",
            "body": "x",
            "state": "open",
            "labels": [{"name": policy.required_label}],
        }
        runner = self.runner(issues={REVIEW_SLUG: [eligible], WORK_SLUG: []})
        issues, _, errors = poller.list_eligible_issues(policy, runner)
        self.assertEqual((issues, errors), ([], []))
        self.assertEqual(runner.repos_queried(), {WORK_SLUG})
        # Full cycle: no implementation task is created for the review repo.
        poller.run_once(policy, self.ledger, runner)
        self.assertFalse(
            any(
                c[c.index("--idempotency-key") + 1].startswith(
                    f"https://github.com/{REVIEW_SLUG}/issues"
                )
                for c in runner.creates
            )
        )

    def test_review_only_repository_is_not_an_implementation_repository(self) -> None:
        policy = self.policy()
        self.assertIsNone(policy.repository_for(REVIEW_SLUG))
        self.assertNotIn(
            REVIEW_SLUG.casefold(), [r.slug.casefold() for r in policy.repositories]
        )
        self.assertIsNotNone(policy.review_repository_for(REVIEW_SLUG))
        self.assertIsNone(policy.review_repository_for(WORK_SLUG))
        # Route targets can only be implementation repositories.
        with self.assertRaises(authority.AuthorityError):
            self.policy(
                issue_routes=[
                    {
                        "label": "route:review",
                        "tracking_repository": WORK_SLUG,
                        "implementation_repository": REVIEW_SLUG,
                    }
                ]
            )

    def test_repair_dispatch_never_considers_the_review_only_repository(self) -> None:
        policy = self.policy()
        runner = self.runner()
        with mock.patch.object(poller, "_open_pull_urls", return_value=set()) as opened:
            poller.reconcile_review_tasks(policy, self.ledger, runner)
        for call in opened.call_args_list:
            self.assertNotEqual(call.args[0].slug, REVIEW_SLUG)

    # -- scope validation at task creation --------------------------------

    def test_task_creation_revalidates_scope(self) -> None:
        policy = self.policy()
        runner = self.runner()
        review_repo = policy.review_repository_for(REVIEW_SLUG)
        assert review_repo is not None
        # A review-only repository cannot claim implementation scope.
        forged = poller.ReviewRequest(review_repo, 7, HEAD, 11, poller.REVIEW_SCOPE_WORK)
        with self.assertRaisesRegex(poller.PollerError, "outside the configured work scope"):
            poller.create_review_task(forged, policy, runner)
        # An unknown repository cannot claim either scope.
        stranger = authority.Repository(OTHER_SLUG, self.root / "demo-repo")
        for scope in (poller.REVIEW_SCOPE_WORK, poller.REVIEW_SCOPE_REVIEW_ONLY, "other"):
            with self.assertRaises(poller.PollerError):
                poller.create_review_task(
                    poller.ReviewRequest(stranger, 1, HEAD, 1, scope), policy, runner
                )
        # A work repository cannot be re-labelled review-only either.
        work_repo = policy.repository_for(WORK_SLUG)
        assert work_repo is not None
        with self.assertRaises(poller.PollerError):
            poller.create_review_task(
                poller.ReviewRequest(work_repo, 3, HEAD, 21, poller.REVIEW_SCOPE_REVIEW_ONLY),
                policy,
                runner,
            )
        self.assertEqual(runner.creates, [])

    def test_allowlisted_review_body_is_unchanged(self) -> None:
        policy = self.policy()
        plain = authority.render_review_task_body(
            pull_url="https://github.com/o/r/pull/1",
            head_sha=HEAD,
            request_url="https://github.com/o/r/pull/1#event-2",
            policy=policy,
        )
        self.assertNotIn("review-only", plain)
        self.assertIn(
            f"allowlist ({WORK_SLUG}). The Captain owns the merge decision.", plain
        )

    # -- ceilings ---------------------------------------------------------

    def test_ceilings_revoke_review_scope_without_widening(self) -> None:
        for cfg in (
            {"enabled": False},
            {"allowed_purposes": ["work"]},
        ):
            with self.subTest(enrollment=cfg):
                policy = self.policy(enrollment=cfg)
                self.assertEqual(policy.review_scope, ())
                runner = self.runner()
                poller.run_once(policy, self.ledger, runner)
                self.assertNotIn(REVIEW_SLUG, runner.repos_queried())
                self.assertEqual(
                    [r.slug for r in policy.repositories], [WORK_SLUG]
                )

    def test_owner_ceiling_and_overlap_are_rejected(self) -> None:
        outside = review_entry(self.root, "other-org/stranger")
        with self.assertRaisesRegex(authority.AuthorityError, "outside github_owners"):
            self.policy(review_repositories=[outside])
        with self.assertRaisesRegex(authority.AuthorityError, "implementation repository"):
            self.policy(
                review_repositories=[
                    {"slug": WORK_SLUG.upper(), "worktree": str(self.root / "elsewhere")}
                ]
            )
        reuse = mapping(self.root)["repositories"][0]  # type: ignore[index]
        with self.assertRaisesRegex(authority.AuthorityError, "implementation worktree"):
            self.policy(
                review_repositories=[{"slug": REVIEW_SLUG, "worktree": reuse["worktree"]}]  # type: ignore[index]
            )
        with self.assertRaisesRegex(authority.AuthorityError, "slugs must be unique"):
            self.policy(
                review_repositories=[
                    review_entry(self.root),
                    {**review_entry(self.root), "worktree": str(self.root / "x")},
                ]
            )
        with self.assertRaisesRegex(authority.AuthorityError, "review_repositories"):
            self.policy(review_repositories="nope")
        with self.assertRaisesRegex(authority.AuthorityError, "review_repositories"):
            self.policy(review_repositories=[{"slug": REVIEW_SLUG, "token": "x"}])

    # -- serialization / projection ---------------------------------------

    def test_scope_survives_serialization_and_stays_separate(self) -> None:
        policy = self.policy(enrollment={"allowed_purposes": ["review", "work"]})
        public = authority.authority_public_dict(policy)
        self.assertEqual([r["slug"] for r in public["repositories"]], [WORK_SLUG])  # type: ignore[index]
        self.assertEqual(
            [r["slug"] for r in public["review_repositories"]], [REVIEW_SLUG]  # type: ignore[index]
        )
        restored = authority.policy_from_mapping(json.loads(json.dumps(public)))
        self.assertEqual(restored.review_repositories, policy.review_repositories)
        self.assertEqual(restored.repositories, policy.repositories)
        # Restricting enrollment still revokes after the round trip.
        public["enrollment"] = {"enabled": True, "allowed_purposes": ["work"]}
        narrowed = authority.policy_from_mapping(public)
        self.assertEqual(narrowed.review_scope, ())
        # A policy without review scope omits the key entirely.
        bare = make_policy(self.root)
        self.assertNotIn("review_repositories", authority.authority_public_dict(bare))

    def test_crew_contract_names_review_only_scope_without_widening_allowlist(self) -> None:
        text = authority.render_crew_contract(self.policy())
        self.assertIn(f"Review-only repositories", text)
        self.assertIn(f"`{REVIEW_SLUG}`", text)
        self.assertIn(f"(`{WORK_SLUG}`) are in scope", text)
        self.assertNotIn("Review-only", authority.render_crew_contract(make_policy(self.root)))

    def test_install_verifies_review_only_worktrees(self) -> None:
        policy = self.policy()
        with mock.patch.object(installer, "_run", return_value="true\n"):
            installer._verify_worktrees(policy)
        missing = self.root / "gone"
        broken = authority.Policy(
            **{
                **{
                    f: getattr(policy, f)
                    for f in (
                        "schedule", "board", "assignee", "github_identity",
                        "inference_provider", "inference_model", "worker_max_turns",
                        "required_label", "repositories", "github_owners", "enrollment",
                    )
                },
                "review_repositories": (authority.Repository(REVIEW_SLUG, missing),),
            }
        )
        with mock.patch.object(installer, "_run", return_value="true\n"):
            with self.assertRaises(installer.InstallError):
                installer._verify_worktrees(broken)


class EnrollmentProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = re_mod.EnrollmentStore(self.root / "enrollments")
        self.policy = make_policy(self.root)
        self.ledger = self.root / "ledger.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def enroll(self, slug: str, purposes: tuple[str, ...], actor: str = CAPTAIN) -> Path:
        checkout = self.root / slug.rsplit("/", 1)[1]
        checkout.mkdir(exist_ok=True)
        self.store.save(
            re_mod.EnrollmentReceipt(
                version=re_mod.RECEIPT_VERSION,
                slug=slug,
                purposes=purposes,
                worktree=str(checkout),
                request_ref=slug,
                actor=actor,
                authorized_at="2026-11-01T00:00:00+00:00",
                created=True,
            )
        )
        return checkout

    def test_review_only_receipts_project_to_review_scope_only(self) -> None:
        self.enroll(REVIEW_SLUG, ("review",))
        self.enroll(OTHER_SLUG, ("work",))  # work enrollment is not review-only
        projected = re_mod.with_review_scope(self.policy, self.store)
        self.assertEqual([r.slug for r in projected.review_repositories], [REVIEW_SLUG])
        self.assertEqual(projected.repositories, self.policy.repositories)
        self.assertEqual([r.slug for r in projected.review_scope], [REVIEW_SLUG])
        # The projection serializes and reloads with the restriction intact.
        restored = authority.policy_from_mapping(
            json.loads(json.dumps(authority.authority_public_dict(projected)))
        )
        self.assertEqual(restored.review_repositories, projected.review_repositories)
        self.assertEqual(restored.repositories, self.policy.repositories)

    def test_stale_or_restricted_receipts_are_not_projected(self) -> None:
        self.enroll(REVIEW_SLUG, ("review",))
        recaptained = make_policy(self.root, captain_github_login="new-captain")
        self.assertEqual(
            re_mod.with_review_scope(recaptained, self.store).review_repositories, ()
        )
        for cfg in ({"enabled": False}, {"allowed_purposes": ["work"]}):
            restricted = make_policy(self.root, enrollment=cfg)
            self.assertEqual(
                re_mod.with_review_scope(restricted, self.store).review_scope, ()
            )
        narrowed = make_policy(
            self.root,
            github_owners=["other-org"],
            repositories=[
                {"slug": "other-org/base", "worktree": str(self.root / "demo-repo")}
            ],
        )
        self.assertEqual(
            re_mod.with_review_scope(narrowed, self.store).review_repositories, ()
        )

    def test_allowlisted_slug_and_collisions_are_never_reclassified(self) -> None:
        # A receipt for an implementation repository adds nothing.
        self.enroll(WORK_SLUG, ("review",))
        self.assertEqual(
            re_mod.with_review_scope(self.policy, self.store).review_repositories, ()
        )
        # A receipt reusing an implementation worktree is skipped.
        self.store.save(
            re_mod.EnrollmentReceipt(
                version=re_mod.RECEIPT_VERSION,
                slug=REVIEW_SLUG,
                purposes=("review",),
                worktree=str(self.policy.repositories[0].worktree),
                request_ref=REVIEW_SLUG,
                actor=CAPTAIN,
                authorized_at="2026-11-01T00:00:00+00:00",
                created=True,
            )
        )
        self.assertEqual(
            re_mod.with_review_scope(self.policy, self.store).review_repositories, ()
        )

    def test_upgraded_work_enrollment_leaves_review_scope_without_dispatch(self) -> None:
        self.enroll(REVIEW_SLUG, ("review", "work"))
        projected = re_mod.with_review_scope(self.policy, self.store)
        # Work enrollment travels the existing helmet-issue path; the poller's
        # review-only projection does not duplicate it.
        self.assertEqual(projected.review_repositories, ())
        self.assertEqual(projected.repositories, self.policy.repositories)

    def test_poller_main_overlays_receipts_only_when_configured(self) -> None:
        self.enroll(REVIEW_SLUG, ("review",))
        seen: list[authority.Policy] = []

        def poll(policy, ledger, runner):
            seen.append(policy)
            return poller.PollResult(created=[], warnings=[], errors=[])

        loader = lambda path: self.policy  # noqa: E731
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("HERMES_HELMET_ENROLLMENT_DIR", None)
            self.assertEqual(poller.main([], policy_loader=loader, poll=poll), 0)
            self.assertEqual(seen[-1].review_repositories, ())
            self.assertEqual(
                poller.main(
                    ["--enrollment-dir", str(self.store.directory)],
                    policy_loader=loader,
                    poll=poll,
                ),
                0,
            )
            self.assertEqual([r.slug for r in seen[-1].review_scope], [REVIEW_SLUG])
            self.assertEqual(seen[-1].repositories, self.policy.repositories)
            os.environ["HERMES_HELMET_ENROLLMENT_DIR"] = str(self.store.directory)
            self.assertEqual(poller.main([], policy_loader=loader, poll=poll), 0)
            self.assertEqual([r.slug for r in seen[-1].review_scope], [REVIEW_SLUG])

    def test_end_to_end_enrolled_review_repository_is_reviewed_not_implemented(self) -> None:
        checkout = self.enroll(REVIEW_SLUG, ("review",))
        projected = re_mod.with_review_scope(self.policy, self.store)
        runner = Runner(
            pulls={REVIEW_SLUG: [pull(7)]},
            events={(REVIEW_SLUG, 7): [request_event(11)]},
            issues={
                REVIEW_SLUG: [
                    {
                        "number": 5,
                        "title": "t",
                        "body": "",
                        "state": "open",
                        "labels": [{"name": projected.required_label}],
                    }
                ]
            },
        )
        result = poller.run_once(projected, self.ledger, runner)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.created, [])
        self.assertEqual(len(result.review_tasks), 1)
        create = runner.creates[0]
        self.assertEqual(create[create.index("--workspace") + 1], f"worktree:{checkout}")
        again = poller.run_once(projected, self.ledger, runner)
        self.assertEqual(len(runner.tasks), 1)
        self.assertEqual(
            [t for _, t in again.review_tasks], [t for _, t in result.review_tasks]
        )


if __name__ == "__main__":
    unittest.main()
