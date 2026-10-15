from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet.authority import policy_from_mapping  # noqa: E402
from hermes_helmet import cli as helmet_cli  # noqa: E402
from hermes_helmet import helmet_issue as hi  # noqa: E402
from hermes_helmet import repo_enroll  # noqa: E402


_WORKER_RUNTIME_PATCHER = mock.patch.object(hi, "DEFAULT_WORKER_RUNTIME", ())


def setUpModule() -> None:
    _WORKER_RUNTIME_PATCHER.start()


def tearDownModule() -> None:
    _WORKER_RUNTIME_PATCHER.stop()


FIXTURE_ORG = "example-org"
FIXTURE_REPO = "demo-repo"
FIXTURE_SLUG = f"{FIXTURE_ORG}/{FIXTURE_REPO}"
ISSUE_URL = f"https://github.com/{FIXTURE_SLUG}/issues/2"
PR_URL = f"https://github.com/{FIXTURE_SLUG}/pull/10"
CAPTAIN = "example-captain"
WORKER = "example-agent"
HEAD = "abc123deadbeef"
PARENT_URL = f"https://github.com/{FIXTURE_SLUG}/issues/42"


def _policy_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 2,
        "company": {"display_name": "ExampleCo", "slug": "exampleco"},
        "captain_github_login": CAPTAIN,
        "worker_github_login": WORKER,
        "schedule": "every 15m",
        "board": "default",
        "assignee": "builder",
        "inference_provider": "openai",
        "inference_model": "gpt-4.1",
        "worker_max_turns": 50,
        "ready_label": "ready-for-agent",
        "dispatch_label": "hermes-kanban-go",
        "cron_deliver": "local",
        "github_owners": [FIXTURE_ORG],
        "trusted_review_bots": ["github-code-quality[bot]"],
        "merge": {
            "default_mode": "unattended_when_clean",
            "unattended_marker": "Merge when clean: yes",
            "narrow_marker": "Merge when clean: no",
        },
        "budgets": {"max_issue_runtime_minutes": 120, "max_repair_rounds": 10},
        "repositories": [
            {"slug": FIXTURE_SLUG, "worktree": "/opt/data/repos/demo-repo"}
        ],
    }
    base.update(overrides)
    return base


def _policy(**overrides: object):
    return policy_from_mapping(_policy_dict(**overrides))


def _ci_rule(*contexts: str, integration_id: int | None = None) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for name in contexts:
        item: dict[str, object] = {"context": name}
        if integration_id is not None:
            item["integration_id"] = integration_id
        items.append(item)
    return {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": items,
            "strict_required_status_checks_policy": False,
        },
    }


def _check(
    name: str,
    *,
    status: str = "completed",
    conclusion: str = "success",
    app_id: int | None = None,
    suite_app_id: int | None = None,
    completed_at: str = "2026-01-01T00:01:00Z",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": completed_at,
    }
    if app_id is not None:
        payload["app"] = {"id": app_id}
    if suite_app_id is not None:
        payload["check_suite"] = {"app": {"id": suite_app_id}}
    return payload


def _status(context: str, state: str = "success") -> dict[str, object]:
    return {"context": context, "state": state}


class MergeApiRunner:
    def __init__(self, slug: str = FIXTURE_SLUG, number: int = 2) -> None:
        self.slug = slug
        self.number = number
        self.identity = CAPTAIN
        self.auth_profiles = [CAPTAIN]
        self.issue_url = f"https://github.com/{slug}/issues/{number}"
        self.pr_url = f"https://github.com/{slug}/pull/10"
        self.head = HEAD
        self.issue = {
            "number": number,
            "title": "Ship it",
            "body": "Ship the loop.\n",
            "state": "open",
            "html_url": self.issue_url,
            "labels": [{"name": "ready-for-agent"}],
        }
        self.pull = {
            "number": 10,
            "html_url": self.pr_url,
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "body": f"Closes #{number}\n\n{self.issue_url}\n",
            "user": {"login": WORKER},
            "head": {"sha": self.head, "ref": f"automation/{slug.rsplit('/', 1)[1]}-{number}"},
            "base": {"ref": "main"},
        }
        self.open_pulls = [self.pull]
        self.timeline: list[object] = []
        self.reviews = [
            {
                "id": 9,
                "user": {"login": CAPTAIN},
                "state": "APPROVED",
                "commit_id": self.head,
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        self.rules: object = [_ci_rule("ci")]
        self.rules_status = 200
        self.protection: object = {"contexts": ["ci"], "checks": [{"context": "ci"}]}
        self.protection_status = 404
        self.check_runs = [_check("ci")]
        self.statuses: list[dict[str, object]] = []
        self.parent_payload: dict[str, object] | None = None
        self.extra_issues: dict[str, dict[str, object]] = {}
        self.fail_merge = False
        self.merge_sets_merged = False
        self.merge_puts = 0
        self.calls: list[list[str]] = []
        self.created_tasks: list[str] = []
        self.pull_sequence: list[dict[str, object]] | None = None
        self.pull_gets = 0

    def run(self, command: list[str]) -> str:
        self.calls.append(list(command))
        if command[:3] == ["gh", "auth", "status"] or (
            command[0].endswith("gh") and command[1:3] == ["auth", "status"]
        ):
            return json.dumps(
                {
                    "hosts": {
                        "github.com": [
                            {"login": login, "active": login == self.identity}
                            for login in self.auth_profiles
                        ]
                    }
                }
            )
        if not (command[0].endswith("gh") and command[1] == "api"):
            raise AssertionError(command)
        if "--method" in command and command[command.index("--method") + 1] == "PUT":
            self.merge_puts += 1
            if self.fail_merge:
                if self.merge_sets_merged:
                    self.pull = dict(self.pull)
                    self.pull["merged"] = True
                    self.pull["state"] = "closed"
                    self.open_pulls = [self.pull]
                raise hi.HelmetIssueError("err:command_failed:gh:exit1:http405")
            self.pull = dict(self.pull)
            self.pull["merged"] = True
            self.pull["state"] = "closed"
            self.open_pulls = [self.pull]
            return json.dumps({"merged": True, "sha": self.pull["head"]["sha"]})
        endpoint = command[-1]
        if endpoint == "user":
            return json.dumps({"login": self.identity})
        if endpoint == f"repos/{self.slug}/issues/{self.number}":
            return json.dumps(self.issue)
        if endpoint in self.extra_issues:
            return json.dumps(self.extra_issues[endpoint])
        if endpoint.startswith(f"repos/{self.slug}/issues/{self.number}/timeline"):
            return json.dumps(self.timeline)
        if endpoint.startswith(f"repos/{self.slug}/pulls?"):
            return json.dumps(self.open_pulls)
        if endpoint.endswith("/parent"):
            if self.parent_payload is None:
                raise hi.HelmetIssueError("err:command_failed:gh:exit1:http404")
            return json.dumps(self.parent_payload)
        if "sub_issues" in endpoint:
            raise hi.HelmetIssueError("err:command_failed:gh:exit1:http404")
        if "/reviews" in endpoint:
            return json.dumps(self.reviews)
        if "/rules/branches/" in endpoint:
            if self.rules_status != 200:
                raise hi.HelmetIssueError(
                    f"err:command_failed:gh:exit1:http{self.rules_status}"
                )
            return json.dumps(self.rules)
        if endpoint.endswith("/protection/required_status_checks"):
            if self.protection_status != 200:
                raise hi.HelmetIssueError(
                    f"err:command_failed:gh:exit1:http{self.protection_status}"
                )
            return json.dumps(self.protection)
        if "/check-runs" in endpoint:
            return json.dumps(
                {"total_count": len(self.check_runs), "check_runs": self.check_runs}
            )
        if "/commits/" in endpoint and endpoint.endswith("/status"):
            return json.dumps({"state": "success", "statuses": self.statuses})
        if endpoint.startswith(f"repos/{self.slug}/pulls/"):
            number = endpoint.rsplit("/", 1)[-1]
            if number == "10":
                self.pull_gets += 1
                return json.dumps(self._pull_payload())
        raise AssertionError(command)

    def _pull_payload(self) -> dict[str, object]:
        if self.pull_sequence and self.pull_gets <= len(self.pull_sequence):
            return self.pull_sequence[self.pull_gets - 1]
        return self.pull


def _ready_checkpoint(**overrides: object) -> hi.Checkpoint:
    payload: dict[str, object] = {
        "version": 1,
        "issue_url": ISSUE_URL,
        "state": "READY",
        "clean_head": HEAD,
        "reviewed_head": HEAD,
        "pr_url": PR_URL,
        "pr_number": 10,
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "merge_mode": "unattended_when_clean",
    }
    payload.update(overrides)
    return hi.Checkpoint(**payload)  # type: ignore[arg-type]


def _merge(
    runner: MergeApiRunner,
    checkpoint_dir: Path,
    *,
    policy=None,
    expected: str = HEAD,
    issue_url: str = ISSUE_URL,
    enrollment_store=None,
    checkpoint: hi.Checkpoint | None = None,
):
    if checkpoint is not None:
        hi.save_checkpoint(hi.checkpoint_path(checkpoint_dir, issue_url), checkpoint)
    return hi.merge_issue(
        issue_url,
        expected_head=expected,
        policy=policy or _policy(),
        ledger=checkpoint_dir / "missing-ledger.sqlite3",
        checkpoint_dir=checkpoint_dir,
        runner=runner,
        gh="gh",
        hermes="hermes",
        enrollment_store=enrollment_store,
    )


class ExactHeadMergeTests(unittest.TestCase):
    def test_matching_approved_head_merges_once_and_cli_reconciles(self) -> None:
        runner = MergeApiRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "policy.json"
            config.write_text(json.dumps(_policy_dict()), encoding="utf-8")
            report = _merge(runner, root / "cp", checkpoint=_ready_checkpoint())
            self.assertEqual(report.decision, "merged")
            self.assertTrue(report.merged)
            self.assertTrue(report.merge_submitted)
            self.assertEqual(runner.merge_puts, 1)
            self.assertEqual(report.checkpoint_state, "DONE")
            saved = hi.load_checkpoint(hi.checkpoint_path(root / "cp", ISSUE_URL))
            assert saved is not None
            self.assertIn("op:merged:abc123deadbe", saved.notes)
            self.assertNotIn("kanban", " ".join(" ".join(call) for call in runner.calls))
            puts_before = runner.merge_puts
            with mock.patch.object(helmet_cli, "SubprocessRunner", return_value=runner):
                code = helmet_cli.main(
                    [
                        "merge",
                        ISSUE_URL,
                        "--expected-head",
                        HEAD,
                        "--config",
                        str(config),
                        "--checkpoint-dir",
                        str(root / "cp"),
                        "--ledger",
                        str(root / "missing.sqlite3"),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(runner.merge_puts, puts_before)
            again = _merge(runner, root / "cp")
            self.assertEqual(again.decision, "reconciled")
            self.assertFalse(again.merge_submitted)
            self.assertEqual(runner.merge_puts, puts_before)

    def test_changed_head_stale_and_revoked_approval_do_not_merge(self) -> None:
        cases = {
            "changed": {"expected": "ffff", "review": "APPROVED", "commit": HEAD},
            "stale": {"expected": HEAD, "review": "APPROVED", "commit": "olderhead"},
            "revoked": {"expected": HEAD, "review": "DISMISSED", "commit": HEAD},
        }
        for name, spec in cases.items():
            with self.subTest(name=name):
                runner = MergeApiRunner()
                runner.reviews = [
                    {
                        "id": 3,
                        "user": {"login": CAPTAIN},
                        "state": spec["review"],
                        "commit_id": spec["commit"],
                        "submitted_at": "2026-01-02T00:00:00Z",
                    }
                ]
                if name == "stale":
                    runner.reviews.insert(
                        0,
                        {
                            "id": 1,
                            "user": {"login": CAPTAIN},
                            "state": "APPROVED",
                            "commit_id": "olderhead",
                            "submitted_at": "2026-01-01T00:00:00Z",
                        },
                    )
                    runner.reviews = [runner.reviews[-1]]
                with tempfile.TemporaryDirectory() as tmp:
                    checkpoint = _ready_checkpoint()
                    if name == "stale":
                        checkpoint.clean_head = "olderhead"
                    report = _merge(
                        runner,
                        Path(tmp),
                        expected=str(spec["expected"]),
                        checkpoint=checkpoint,
                    )
                self.assertFalse(report.merged)
                self.assertFalse(report.merge_submitted)
                self.assertEqual(runner.merge_puts, 0)
                self.assertTrue(report.blocker)

    def test_blocking_review_draft_and_conflict_cannot_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = MergeApiRunner()
            report = _merge(
                runner,
                Path(tmp),
                checkpoint=_ready_checkpoint(state="BLOCKED", clean_head=None),
            )
            self.assertEqual(runner.merge_puts, 0)
            self.assertIn("review", report.blocker or "")

        with tempfile.TemporaryDirectory() as tmp:
            runner = MergeApiRunner()
            runner.pull = dict(runner.pull)
            runner.pull["draft"] = True
            runner.pull["mergeable_state"] = "draft"
            runner.open_pulls = [runner.pull]
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
            self.assertEqual(runner.merge_puts, 0)
            self.assertIn("draft", report.blocker or "")

        with tempfile.TemporaryDirectory() as tmp:
            runner = MergeApiRunner()
            runner.pull = dict(runner.pull)
            runner.pull["mergeable_state"] = "dirty"
            runner.open_pulls = [runner.pull]
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
            self.assertEqual(runner.merge_puts, 0)
            self.assertIn("pr_not_mergeable", report.blocker or "")

    def test_failed_pending_missing_and_unknown_checks_cannot_merge(self) -> None:
        variants = [
            ("failed", [_check("ci", conclusion="failure")], [], "required_check_failed"),
            ("pending", [_check("ci", status="in_progress", conclusion="")], [], "required_check_pending"),
            ("missing", [], [], "required_check_missing"),
            ("unknown", [_check("ci", conclusion="bogus")], [], "merge_check_state_unknown"),
        ]
        for name, runs, statuses, needle in variants:
            with self.subTest(name=name):
                runner = MergeApiRunner()
                runner.check_runs = runs
                runner.statuses = statuses
                with tempfile.TemporaryDirectory() as tmp:
                    report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
                self.assertEqual(runner.merge_puts, 0)
                self.assertIn(needle, report.blocker or "")
                self.assertFalse(report.merged)

    def test_clean_mergeable_state_without_check_evidence_cannot_merge(self) -> None:
        runner = MergeApiRunner()
        runner.rules_status = 403
        runner.protection_status = 403
        runner.pull = dict(runner.pull)
        runner.pull["mergeable_state"] = "clean"
        runner.open_pulls = [runner.pull]
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("merge_check_evidence_missing", report.blocker or "")

    def test_unknown_mergeable_state_cannot_merge(self) -> None:
        runner = MergeApiRunner()
        runner.pull = dict(runner.pull)
        runner.pull["mergeable_state"] = "unknown"
        runner.open_pulls = [runner.pull]
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("merge_live_state_unknown", report.blocker or "")

    def test_empty_configured_checks_do_not_invent_requirements(self) -> None:
        runner = MergeApiRunner()
        runner.rules = []
        runner.check_runs = [_check("optional", conclusion="failure")]
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertTrue(report.merged)
        self.assertEqual(runner.merge_puts, 1)

    def test_local_only_checkless_clean_requires_approval(self) -> None:
        runner = MergeApiRunner()
        runner.rules = []
        policy = _policy(worker_completion_contract="local-only")
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                policy=policy,
                checkpoint=_ready_checkpoint(),
            )
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

    def test_local_only_verified_required_check_can_merge(self) -> None:
        runner = MergeApiRunner()
        policy = _policy(worker_completion_contract="local-only")
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                policy=policy,
                checkpoint=_ready_checkpoint(),
            )
        self.assertTrue(report.merged)
        self.assertEqual(runner.merge_puts, 1)

    def test_policy_ceiling_and_issue_marker_require_approval(self) -> None:
        raw = _policy_dict()
        raw["merge"] = {
            "default_mode": "explicit_captain_approval",
            "unattended_marker": "Merge when clean: yes",
            "narrow_marker": "Merge when clean: no",
        }
        runner = MergeApiRunner()
        runner.issue = dict(runner.issue)
        runner.issue["body"] = "Merge when clean: yes\n"
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                policy=policy_from_mapping(raw),
                checkpoint=_ready_checkpoint(),
            )
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

        runner = MergeApiRunner()
        runner.issue = dict(runner.issue)
        runner.issue["body"] = "Merge when clean: no\n"
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

    def test_live_parent_narrow_marker_and_child_reenable(self) -> None:
        runner = MergeApiRunner()
        runner.parent_payload = {"html_url": PARENT_URL}
        runner.extra_issues[f"repos/{FIXTURE_SLUG}/issues/42"] = {
            "number": 42,
            "title": "Epic",
            "body": "Merge when clean: no\n",
            "state": "open",
            "html_url": PARENT_URL,
            "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

        runner = MergeApiRunner()
        runner.issue = dict(runner.issue)
        runner.issue["body"] = "Merge when clean: yes\n"
        runner.parent_payload = {"html_url": PARENT_URL}
        runner.extra_issues[f"repos/{FIXTURE_SLUG}/issues/42"] = {
            "number": 42,
            "title": "Epic",
            "body": "Merge when clean: no\n",
            "state": "open",
            "html_url": PARENT_URL,
            "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertTrue(report.merged)
        self.assertEqual(runner.merge_puts, 1)

    def test_moved_parent_drops_stale_permissive_association(self) -> None:
        stale = f"https://github.com/{FIXTURE_SLUG}/issues/7"
        runner = MergeApiRunner()
        runner.parent_payload = {"html_url": PARENT_URL}
        runner.extra_issues[f"repos/{FIXTURE_SLUG}/issues/42"] = {
            "number": 42,
            "title": "Epic",
            "body": "Merge when clean: no\n",
            "state": "open",
            "html_url": PARENT_URL,
            "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                checkpoint=_ready_checkpoint(parent_epic_url=stale),
            )
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("explicit_captain_approval_required", report.blocker or "")

    def test_removed_parent_does_not_keep_stale_narrow_marker(self) -> None:
        runner = MergeApiRunner()
        runner.parent_payload = None
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                checkpoint=_ready_checkpoint(parent_epic_url=PARENT_URL),
            )
        self.assertTrue(report.merged)
        self.assertEqual(runner.merge_puts, 1)

    def test_review_only_and_missing_enrollment_cannot_merge(self) -> None:
        slug = "example-org/enrolled-repo"
        issue_url = f"https://github.com/{slug}/issues/7"
        runner = MergeApiRunner(slug, 7)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = repo_enroll.EnrollmentStore(root / "enroll")
            store.save(
                repo_enroll.EnrollmentReceipt(
                    version=repo_enroll.RECEIPT_VERSION,
                    slug=slug,
                    purposes=("review",),
                    worktree="/opt/data/repos/enrolled-repo",
                    request_ref=slug,
                    actor=CAPTAIN,
                    authorized_at="2026-09-30T00:00:00+00:00",
                    created=True,
                )
            )
            checkpoint = _ready_checkpoint(issue_url=issue_url, pr_url=runner.pr_url)
            report = _merge(
                runner,
                root / "cp",
                issue_url=issue_url,
                enrollment_store=store,
                checkpoint=checkpoint,
            )
            self.assertEqual(runner.merge_puts, 0)
            self.assertIn("review_enrollment_no_merge", report.blocker or "")
            denied = repo_enroll.EnrollmentStore(root / "denied")
            with self.assertRaises(hi.HelmetIssueError):
                _merge(
                    MergeApiRunner(slug, 7),
                    root / "cp2",
                    issue_url=issue_url,
                    enrollment_store=denied,
                    checkpoint=checkpoint,
                )

    def test_exhausted_original_issue_and_epic_budgets_cannot_merge(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(minutes=121)).replace(microsecond=0)
        runner = MergeApiRunner()
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(
                runner,
                Path(tmp),
                checkpoint=_ready_checkpoint(started_at=old.isoformat()),
            )
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("max_issue_runtime_minutes", report.blocker or "")

        from hermes_helmet import helmet_epic as he

        runner = MergeApiRunner()
        runner.parent_payload = {"html_url": PARENT_URL}
        runner.extra_issues[f"repos/{FIXTURE_SLUG}/issues/42"] = {
            "number": 42,
            "title": "Epic",
            "body": "Parent\n",
            "state": "open",
            "html_url": PARENT_URL,
            "labels": [],
        }
        policy = _policy(
            budgets={
                "max_issue_runtime_minutes": 120,
                "max_epic_runtime_minutes": 8640,
                "max_repair_rounds": 10,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            he.save_epic_checkpoint(
                he.epic_checkpoint_path(root, PARENT_URL),
                he.EpicCheckpoint(
                    version=1,
                    epic_url=PARENT_URL,
                    state="WAITING",
                    started_at=(
                        datetime.now(timezone.utc) - timedelta(minutes=8641)
                    ).replace(microsecond=0).isoformat(),
                ),
            )
            report = _merge(
                runner,
                root,
                policy=policy,
                checkpoint=_ready_checkpoint(parent_epic_url=PARENT_URL),
            )
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("max_epic_runtime_minutes", report.blocker or "")

    def test_api_error_reconciles_confirmed_merge_and_unmerged_does_not_retry(self) -> None:
        runner = MergeApiRunner()
        runner.fail_merge = True
        runner.merge_sets_merged = True
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 1)
        self.assertTrue(report.merged)
        self.assertEqual(report.decision, "merged")
        self.assertTrue(report.merge_submitted)

        runner = MergeApiRunner()
        runner.fail_merge = True
        runner.merge_sets_merged = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = _merge(runner, root, checkpoint=_ready_checkpoint())
            self.assertEqual(runner.merge_puts, 1)
            self.assertFalse(report.merged)
            self.assertIn("merge_failed_not_merged", report.blocker or "")
            again = _merge(runner, root)
            self.assertEqual(runner.merge_puts, 1)
            self.assertIn("merge_already_attempted", again.blocker or "")
            self.assertFalse(again.merge_submitted)

    def test_protection_fallback_and_legacy_status_are_actual_checks(self) -> None:
        runner = MergeApiRunner()
        runner.rules_status = 404
        runner.protection_status = 200
        runner.protection = {"contexts": ["legacy"], "checks": [{"context": "legacy"}]}
        runner.check_runs = []
        runner.statuses = [{"context": "legacy", "state": "success"}]
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertTrue(report.merged)
        self.assertEqual(runner.merge_puts, 1)

        runner = MergeApiRunner()
        runner.rules_status = 404
        runner.protection_status = 200
        runner.protection = {"contexts": ["legacy"], "checks": [{"context": "legacy"}]}
        runner.check_runs = []
        runner.statuses = [{"context": "legacy", "state": "failure"}]
        with tempfile.TemporaryDirectory() as tmp:
            report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
        self.assertEqual(runner.merge_puts, 0)
        self.assertIn("required_check_failed", report.blocker or "")

    def test_documented_command_is_the_supported_operation(self) -> None:
        skill = (ROOT / "skills/helmet-issue/SKILL.md").read_text(encoding="utf-8")
        delivery = (ROOT / "skills/helmet-issue/references/delivery.md").read_text(
            encoding="utf-8"
        )
        reference = (ROOT / "docs/helmet-issue.md").read_text(encoding="utf-8")
        for text in (skill, delivery, reference):
            self.assertIn("helmet merge ISSUE_URL --expected-head SHA", text)
        self.assertNotIn("second queue", delivery.casefold())
        source = (ROOT / "src/hermes_helmet/helmet_issue.py").read_text(encoding="utf-8")
        self.assertIn("def merge_issue(", source)
        self.assertIn("evaluate_merge_gate(", source)
        self.assertIn("merge_pull_request(", source)

    def test_same_name_sources_app_identity_and_neutral_follow_github(self) -> None:
        passing = [
            (
                "both_sources_pass",
                [_check("ci")],
                [_status("ci")],
                [_ci_rule("ci")],
                None,
            ),
            (
                "neutral",
                [_check("ci", conclusion="neutral")],
                [],
                [_ci_rule("ci")],
                None,
            ),
            (
                "skipped",
                [_check("ci", conclusion="skipped")],
                [],
                [_ci_rule("ci")],
                None,
            ),
            (
                "expected_app",
                [_check("ci", app_id=123)],
                [],
                [_ci_rule("ci", integration_id=123)],
                None,
            ),
            (
                "expected_app_via_suite",
                [_check("ci", suite_app_id=123)],
                [],
                [_ci_rule("ci", integration_id=123)],
                None,
            ),
            (
                "any_app_sentinel",
                [_check("ci", app_id=456)],
                [],
                [_ci_rule("ci", integration_id=-1)],
                None,
            ),
            (
                "other_app_failure_does_not_override_expected_app",
                [
                    _check("ci", app_id=123, completed_at="2026-01-01T00:01:00Z"),
                    _check(
                        "ci",
                        app_id=456,
                        conclusion="failure",
                        completed_at="2026-01-01T00:02:00Z",
                    ),
                ],
                [],
                [_ci_rule("ci", integration_id=123)],
                None,
            ),
            (
                "protection_app_match",
                [_check("ci", app_id=123)],
                [],
                None,
                {
                    "contexts": ["ci"],
                    "checks": [{"context": "ci", "app_id": 123}],
                },
            ),
        ]
        withholding = [
            (
                "status_fails",
                [_check("ci")],
                [_status("ci", "failure")],
                [_ci_rule("ci")],
                None,
                "required_check_failed",
            ),
            (
                "check_fails_status_passes",
                [_check("ci", conclusion="failure")],
                [_status("ci")],
                [_ci_rule("ci")],
                None,
                "required_check_failed",
            ),
            (
                "status_pending",
                [_check("ci")],
                [_status("ci", "pending")],
                [_ci_rule("ci")],
                None,
                "required_check_pending",
            ),
            (
                "wrong_app",
                [_check("ci", app_id=456)],
                [],
                [_ci_rule("ci", integration_id=123)],
                None,
                "required_check_missing",
            ),
            (
                "status_does_not_replace_expected_app",
                [],
                [_status("ci")],
                [_ci_rule("ci", integration_id=123)],
                None,
                "required_check_missing",
            ),
            (
                "expected_app_and_failing_status",
                [_check("ci", app_id=123)],
                [_status("ci", "failure")],
                [_ci_rule("ci", integration_id=123)],
                None,
                "required_check_failed",
            ),
            (
                "protection_wrong_app",
                [_check("ci", app_id=456)],
                [],
                None,
                {
                    "contexts": ["ci"],
                    "checks": [{"context": "ci", "app_id": 123}],
                },
                "required_check_missing",
            ),
            (
                "unusable_app_constraint",
                [_check("ci")],
                [],
                [_ci_rule("ci", integration_id="123")],  # type: ignore[arg-type]
                None,
                "merge_check_state_unknown",
            ),
        ]
        for name, runs, statuses, rules, protection in passing:
            with self.subTest(name=name):
                runner = MergeApiRunner()
                runner.check_runs = runs
                runner.statuses = statuses
                if rules is None:
                    runner.rules_status = 404
                    runner.protection_status = 200
                    runner.protection = protection
                else:
                    runner.rules = rules
                with tempfile.TemporaryDirectory() as tmp:
                    report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
                self.assertTrue(report.merged, report.blocker)
                self.assertEqual(runner.merge_puts, 1)
        for name, runs, statuses, rules, protection, needle in withholding:
            with self.subTest(name=name):
                runner = MergeApiRunner()
                runner.check_runs = runs
                runner.statuses = statuses
                if rules is None:
                    runner.rules_status = 404
                    runner.protection_status = 200
                    runner.protection = protection
                else:
                    runner.rules = rules
                with tempfile.TemporaryDirectory() as tmp:
                    report = _merge(runner, Path(tmp), checkpoint=_ready_checkpoint())
                self.assertEqual(runner.merge_puts, 0)
                self.assertFalse(report.merged)
                self.assertIn(needle, report.blocker or "")

    def test_cli_accepts_neutral_required_check(self) -> None:
        runner = MergeApiRunner()
        runner.check_runs = [_check("ci", conclusion="neutral")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "policy.json"
            config.write_text(json.dumps(_policy_dict()), encoding="utf-8")
            hi.save_checkpoint(
                hi.checkpoint_path(root / "cp", ISSUE_URL),
                _ready_checkpoint(),
            )
            with mock.patch.object(helmet_cli, "SubprocessRunner", return_value=runner):
                code = helmet_cli.main(
                    [
                        "merge",
                        ISSUE_URL,
                        "--expected-head",
                        HEAD,
                        "--config",
                        str(config),
                        "--checkpoint-dir",
                        str(root / "cp"),
                        "--ledger",
                        str(root / "missing.sqlite3"),
                        "--json",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(runner.merge_puts, 1)

    def test_final_pull_read_withholds_newly_negative_state(self) -> None:
        cases = [
            ("dirty", {"mergeable_state": "dirty"}, "pr_not_mergeable"),
            ("blocked", {"mergeable_state": "blocked"}, "pr_not_mergeable"),
            ("behind", {"mergeable_state": "behind"}, "pr_not_mergeable"),
            ("unstable", {"mergeable_state": "unstable"}, "pr_not_mergeable"),
            ("unknown", {"mergeable_state": "unknown"}, "merge_live_state_unknown"),
            ("blank", {"mergeable_state": ""}, "merge_live_state_unknown"),
            ("draft", {"draft": True, "mergeable_state": "draft"}, "pr_is_draft"),
            ("draft_flag", {"draft": True, "mergeable_state": "clean"}, "pr_is_draft"),
        ]
        for name, patch, needle in cases:
            with self.subTest(name=name):
                runner = MergeApiRunner()
                clean = dict(runner.pull)
                changed = dict(runner.pull)
                changed.update(patch)
                runner.pull_sequence = [clean, clean, changed]
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    report = _merge(runner, root, checkpoint=_ready_checkpoint())
                    saved = hi.load_checkpoint(hi.checkpoint_path(root, ISSUE_URL))
                self.assertEqual(runner.merge_puts, 0)
                self.assertEqual(runner.pull_gets, 3)
                self.assertFalse(report.merged)
                self.assertFalse(report.merge_submitted)
                self.assertIn(needle, report.blocker or "")
                assert saved is not None
                self.assertIsNone(saved.merge_attempted_head)


if __name__ == "__main__":
    unittest.main()
