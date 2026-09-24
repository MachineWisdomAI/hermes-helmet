#!/usr/bin/env python3
"""Runtime dogfood evidence contract tests for a bound private RC."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_helmet import cli as helmet_cli
from hermes_helmet import dogfood as df
from hermes_helmet import release_candidate as rc
from hermes_helmet.authority import load_authority


ROOT = Path(__file__).resolve().parents[1]
BOUND_PATH = ROOT / "config" / "dogfood-bound.json"
POLICY_PATH = ROOT / "config" / "policy.example.json"
RECEIPTS_PATH = ROOT / "config" / "dogfood-receipts"
RC_SOURCE = "b2fed3dae7a9e5411511b1604e06a8fecbed35e8"
RC_DIGEST = "sha256:d37c470ed80a86bc7fd4b32430fff432fca05001ee6e0eb221347b267df9aa44"
EXAMPLE_SOURCE = "0123456789abcdef0123456789abcdef01234567"
EXAMPLE_DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
TASK_ID = "t_0123456789abcdef"
PR_URL = "https://github.com/example-org/demo-repo/pull/48"
PRIOR_REPAIR = PR_URL
OTHER_PR_URL = "https://github.com/example-org/demo-repo/pull/47"
ISSUE_URL = "https://github.com/example-org/demo-repo/issues/9"
EPIC_URL = "https://github.com/example-org/demo-repo/issues/42"
CROSS_EPIC_URL = "https://github.com/example-org/control-plane/issues/42"
SECRET_VALUE = "ghp_should_not_appear_in_errors"
CHECK_OUTPUTS = {
    "setup": "setup\n",
    "doctor": "doctor\n",
    "status_readonly": "status\n",
    "persistence": "persist\n",
    "skills": "skills\n",
    "no_skills": "noskills\n",
    "default_integrations": "integrations\n",
    "epic_resume": "epic-resume\n",
    "issue_dispatch": "issue-dispatch\n",
}


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _policy(path: Path = POLICY_PATH):
    return load_authority(path)


def _bound(path: Path = BOUND_PATH, policy_path: Path = POLICY_PATH) -> df.BoundCandidate:
    return df.load_bound_candidate(path, policy=_policy(policy_path))


def _trusted_github() -> dict[str, object]:
    return df.load_github_evidence(RECEIPTS_PATH / "github.json", _bound())


def _bind(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "bound": _bound(),
        "receipts_dir": RECEIPTS_PATH,
        "expected_head": HEAD_B,
    }
    values.update(overrides)
    return values


def _review(
    *,
    head: str,
    outcome: str,
    reviewed_at: str,
    review_id: int,
    prior_repair: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "head": head,
        "outcome": outcome,
        "reviewed_at": reviewed_at,
        "review_id": review_id,
        "reviewer": "example-captain",
    }
    if prior_repair is not None:
        payload["prior_repair"] = prior_repair
    return payload


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": "0.1.0rc1",
        "channel": "private-rc",
        "minted_version": "0.1.0",
        "source_revision": EXAMPLE_SOURCE,
        "image": {
            "repository": "ghcr.io/example-org/hermes-helmet",
            "digest": EXAMPLE_DIGEST,
            "version": "0.1.0rc1",
            "tags": [f"dev-{EXAMPLE_SOURCE}", "0.1.0rc1"],
        },
        "task_id": TASK_ID,
        "published_pr": PR_URL,
        "current_head": HEAD_B,
        "merge": {
            "policy": "captain_approval",
            "when_clean": False,
            "directive": None,
        },
        "reviews": [
            _review(
                head=HEAD_A,
                outcome="changes_requested",
                reviewed_at="2026-09-21T16:00:00+00:00",
                review_id=1001,
            ),
            _review(
                head=HEAD_B,
                outcome="clean",
                reviewed_at="2026-09-21T16:20:00+00:00",
                review_id=1002,
                prior_repair=PRIOR_REPAIR,
            ),
        ],
        "repairs": [
            {
                "published_pr": PR_URL,
                "head_before": HEAD_A,
                "head_after": HEAD_B,
                "force_push": False,
                "repaired_at": "2026-09-21T16:10:00+00:00",
            }
        ],
        "runtime": {
            "dogfood_host": "codex",
            "claude_required": False,
            "hermes_host_required": False,
        },
    }
    payload.update(overrides)
    return payload


def _write_receipt(
    directory: Path,
    name: str,
    *,
    command: list[str] | None = None,
    exit_code: int = 0,
    output: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    expected = {
        **df.CODEX_CHECK_COMMANDS,
        "epic_resume": ("hermes-helmet", "epic", EPIC_URL),
        "issue_dispatch": ("hermes-helmet", "issue", ISSUE_URL),
    }
    payload: dict[str, object] = {
        "command": list(command or expected[name]),
        "exit_code": exit_code,
        "observed_at": "2026-09-21T15:00:00+00:00",
        "output": CHECK_OUTPUTS[name] if output is None else output,
    }
    if extra:
        payload.update(extra)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _copy_receipts(directory: Path) -> Path:
    destination = directory / "receipts"
    shutil.copytree(RECEIPTS_PATH, destination)
    return destination


def _live_github_run_mocks(
    *,
    prior_reviews: list[dict[str, object]] | None = None,
    prior_ok: bool = True,
) -> list[mock.Mock]:
    fake_pull = mock.Mock(
        returncode=0,
        stdout=json.dumps(
            {
                "html_url": PR_URL,
                "number": 48,
                "head": {"sha": HEAD_B, "ref": "automation/demo-issue-9"},
            }
        ),
        stderr="",
    )
    fake_reviews = mock.Mock(
        returncode=0, stdout=json.dumps(_trusted_github()["reviews"]), stderr=""
    )
    fake_commits = mock.Mock(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "sha": item["sha"],
                    "commit": {"committer": {"date": item["committed_at"]}},
                }
                for item in _trusted_github()["commits"]
            ]
        ),
        stderr="",
    )
    if not prior_ok:
        missing = mock.Mock(returncode=1, stdout="", stderr="Not Found")
        return [fake_pull, fake_reviews, fake_commits, missing]
    prior_pull = mock.Mock(
        returncode=0,
        stdout=json.dumps(
            {
                "html_url": OTHER_PR_URL,
                "number": 47,
                "head": {"sha": HEAD_C, "ref": "automation/demo-issue-8"},
            }
        ),
        stderr="",
    )
    if prior_reviews is None:
        prior_reviews = [
            {
                "id": 9001,
                "user": {"login": "example-captain"},
                "commit_id": HEAD_A,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-09-20T16:00:00+00:00",
                "html_url": f"{OTHER_PR_URL}#pullrequestreview-9001",
            }
        ]
    prior_reviews_mock = mock.Mock(
        returncode=0, stdout=json.dumps(prior_reviews), stderr=""
    )
    prior_commits = mock.Mock(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "sha": HEAD_A,
                    "commit": {"committer": {"date": "2026-09-20T15:50:00+00:00"}},
                },
                {
                    "sha": HEAD_C,
                    "commit": {"committer": {"date": "2026-09-20T16:10:00+00:00"}},
                },
            ]
        ),
        stderr="",
    )
    return [
        fake_pull,
        fake_reviews,
        fake_commits,
        prior_pull,
        prior_reviews_mock,
        prior_commits,
    ]


class VersionAndDocsTests(unittest.TestCase):
    def test_bound_manifest_pins_exampleco_rc_not_stable_release(self) -> None:
        bound = _bound()
        self.assertEqual(bound.version, "0.1.0rc1")
        self.assertEqual(bound.source_revision, EXAMPLE_SOURCE)
        self.assertEqual(bound.image_digest, EXAMPLE_DIGEST)
        self.assertEqual(bound.version, rc.PACKAGE_VERSION)
        self.assertNotEqual(bound.version, rc.MINTED_VERSION)
        self.assertEqual(bound.dogfood_host, "codex")
        self.assertEqual(bound.github_repository, "example-org/demo-repo")
        self.assertEqual(bound.captain_github_login, "example-captain")
        self.assertEqual(bound.wrapper, "example-wrapper")
        self.assertEqual(bound.epic_url, EPIC_URL)
        self.assertEqual(bound.issue_url, ISSUE_URL)
        self.assertEqual(
            hashlib.sha256(BOUND_PATH.read_bytes()).hexdigest(),
            df.CANONICAL_BOUND_SHA256,
        )
        self.assertEqual(
            df.trusted_receipts_sha256(RECEIPTS_PATH),
            bound.trusted_receipts_sha256,
        )

    def test_public_dogfood_fixtures_use_exampleco_identities(self) -> None:
        blob = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                BOUND_PATH,
                ROOT / "config" / "dogfood-evidence.example.json",
                RECEIPTS_PATH / "observations.json",
                RECEIPTS_PATH / "github.json",
                RECEIPTS_PATH / "epic_resume.json",
                RECEIPTS_PATH / "issue_dispatch.json",
            )
        )
        self.assertIn("example-org/demo-repo", blob)
        self.assertIn("example-captain", blob)
        self.assertIn("example-wrapper", blob)
        self.assertNotIn("MachineWisdomAI", blob)
        self.assertNotIn("machinewisdomai", blob)
        self.assertFalse((ROOT / "config" / "dogfood-github.example.json").exists())

    def test_docs_record_codex_dogfood_without_minting_stable_release(self) -> None:
        docs = _read("docs/dogfood.md")
        self.assertIn("0.1.0rc1", docs)
        self.assertIn(RC_DIGEST, docs)
        self.assertIn(RC_SOURCE, docs)
        self.assertIn("codex", docs.casefold())
        self.assertIn("setup", docs.casefold())
        self.assertIn("doctor", docs.casefold())
        self.assertIn("status", docs.casefold())
        self.assertIn("rollback", docs.casefold())
        self.assertIn("metadata.published_pr", docs)
        self.assertIn("does not mint", docs.casefold())
        self.assertIn("dogfood-bound.json", docs)
        self.assertIn("policy.example.json", docs)
        self.assertIn("dogfood-receipts", docs)
        self.assertIn("private overlay", docs.casefold())
        self.assertNotRegex(docs, r"(?i)public (v)?0\.1\.0")
        self.assertNotRegex(docs, r"\bH11\b")
        self.assertNotRegex(docs, r"\bH13\b")
        changelog = _read("CHANGELOG.md")
        preview = changelog.split("## Public source preview", 1)[1].split("\n## ", 1)[0]
        self.assertIn("dogfood", preview.casefold())
        self.assertIn("0.1.0rc1", preview)

    def test_example_evidence_validates_against_bound_manifest(self) -> None:
        path = ROOT / "config" / "dogfood-evidence.example.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        validated = df.validate_runtime_evidence(payload, **_bind())
        self.assertEqual(validated["image"]["digest"], EXAMPLE_DIGEST)
        self.assertEqual(validated["current_head"], HEAD_B)
        self.assertEqual(validated["branch"], "automation/demo-issue-9")
        self.assertFalse(validated["worker"]["merged"])
        self.assertNotIn("api_token", validated)
        self.assertEqual(validated["reviews"][0]["review_id"], 1001)
        self.assertEqual(validated["reviews"][0]["reviewer"], "example-captain")
        self.assertEqual(validated["checkpoint"]["skill"], "helmet-epic")
        self.assertEqual(validated["dispatch"]["wrapper"], "example-wrapper")
        self.assertEqual(
            validated["runtime"]["checks"]["setup"]["output_sha256"],
            hashlib.sha256(b"setup\n").hexdigest(),
        )


class EvidenceSchemaTests(unittest.TestCase):
    def test_valid_clean_run_cites_prior_repair_without_manufacturing(self) -> None:
        payload = df.validate_runtime_evidence(_valid_payload(), **_bind())
        self.assertEqual(payload["task_id"], TASK_ID)
        self.assertEqual(payload["published_pr"], PR_URL)
        self.assertEqual(payload["reviews"][-1]["prior_repair"], PRIOR_REPAIR)
        self.assertEqual(payload["reviews"][-1]["review_id"], 1002)
        self.assertEqual(payload["checkpoint"]["resumed"], True)
        self.assertEqual(payload["dispatch"]["skill"], "helmet-issue")
        self.assertEqual(payload["dispatch"]["issue_url"], ISSUE_URL)

    def test_build_runtime_evidence_requires_observed_receipts(self) -> None:
        built = df.build_runtime_evidence(
            bound=_bound(),
            task_id=TASK_ID,
            published_pr=PR_URL,
            current_head=HEAD_B,
            reviews=_valid_payload()["reviews"],
            receipts_dir=RECEIPTS_PATH,
            expected_head=HEAD_B,
            repairs=_valid_payload()["repairs"],
        )
        self.assertEqual(built["source_revision"], EXAMPLE_SOURCE)
        self.assertEqual(built["image"]["digest"], EXAMPLE_DIGEST)
        self.assertEqual(
            built["runtime"]["checks"]["doctor"]["output_sha256"],
            hashlib.sha256(b"doctor\n").hexdigest(),
        )
        again = df.validate_runtime_evidence(_valid_payload(), **_bind())
        self.assertEqual(again, built)

    def test_owner_bound_manifest_is_accepted_when_policy_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bound.json"
            payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
            payload["image_digest"] = "sha256:" + ("ab" * 32)
            path.write_text(json.dumps(payload), encoding="utf-8")
            bound = df.load_bound_candidate(path, policy=_policy())
            self.assertEqual(bound.image_digest, payload["image_digest"])
            self.assertEqual(bound.captain_github_login, "example-captain")
            self.assertEqual(bound.github_repository, "example-org/demo-repo")

    def test_owner_bound_manifest_cannot_redefine_private_rc_invariants(self) -> None:
        mutations = {
            "version": "0.1.0",
            "channel": "stable",
            "minted_version": "9.9.9",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "bound.json"
                payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
                payload[field] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(df.DogfoodError):
                    df.load_bound_candidate(path, policy=_policy())

    def test_replacement_bound_manifest_is_rejected_without_policy_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bound.json"
            payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
            payload["captain_github_login"] = "other-captain"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(df.DogfoodError):
                df.load_bound_candidate(path, policy=_policy())
            payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
            payload["github_repository"] = "example-org/other-repo"
            payload["issue_url"] = "https://github.com/example-org/other-repo/issues/9"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(df.DogfoodError):
                df.load_bound_candidate(path, policy=_policy())

    def test_cross_repository_epic_is_accepted_when_policy_allows_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            policy["repositories"].append(
                {
                    "slug": "example-org/control-plane",
                    "worktree": "/opt/data/repos/control-plane",
                }
            )
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            receipts = _copy_receipts(root)
            _write_receipt(
                receipts,
                "epic_resume",
                command=["hermes-helmet", "epic", CROSS_EPIC_URL],
            )
            observations = json.loads((receipts / "observations.json").read_text(encoding="utf-8"))
            observations["checkpoint"]["epic_url"] = CROSS_EPIC_URL
            (receipts / "observations.json").write_text(json.dumps(observations), encoding="utf-8")
            bound_payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
            bound_payload["epic_url"] = CROSS_EPIC_URL
            bound_payload["trusted_receipts_sha256"] = df.trusted_receipts_sha256(receipts)
            bound_path = root / "bound.json"
            bound_path.write_text(json.dumps(bound_payload), encoding="utf-8")
            bound = df.load_bound_candidate(bound_path, policy=_policy(policy_path))
            self.assertEqual(bound.epic_url, CROSS_EPIC_URL)
            self.assertEqual(bound.issue_url, ISSUE_URL)
            self.assertEqual(bound.github_repository, "example-org/demo-repo")
            validated = df.validate_runtime_evidence(
                _valid_payload(),
                **_bind(bound=bound, receipts_dir=receipts),
            )
            self.assertEqual(validated["checkpoint"]["epic_url"], CROSS_EPIC_URL)
            self.assertEqual(validated["dispatch"]["issue_url"], ISSUE_URL)

    def test_cross_repository_epic_is_rejected_when_policy_omits_epic_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bound.json"
            payload = json.loads(BOUND_PATH.read_text(encoding="utf-8"))
            payload["epic_url"] = CROSS_EPIC_URL
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(df.DogfoodError) as ctx:
                df.load_bound_candidate(path, policy=_policy())
            self.assertIn("epic URL", str(ctx.exception))

    def test_mutated_receipts_are_not_the_trusted_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            _write_receipt(receipts, "doctor", output="mutated-doctor\n")
            with self.assertRaises(df.DogfoodError) as ctx:
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))
            self.assertIn("trusted receipt set", str(ctx.exception))

    def test_stable_version_is_rejected(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(version="0.1.0"), **_bind())
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    image={
                        "repository": "ghcr.io/example-org/hermes-helmet",
                        "digest": EXAMPLE_DIGEST,
                        "version": "0.1.0",
                        "tags": ["0.1.0"],
                    }
                ),
                **_bind(),
            )

    def test_unauthorized_digest_or_source_is_rejected(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(source_revision="0" * 40), **_bind()
            )
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    image={
                        "repository": "ghcr.io/example-org/hermes-helmet",
                        "digest": "sha256:" + ("ab" * 32),
                        "version": "0.1.0rc1",
                        "tags": [f"dev-{EXAMPLE_SOURCE}"],
                    }
                ),
                **_bind(),
            )

    def test_worker_must_not_merge_or_force_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            observations = json.loads((receipts / "observations.json").read_text(encoding="utf-8"))
            observations["worker"] = {"merged": True, "force_pushed": False}
            (receipts / "observations.json").write_text(json.dumps(observations), encoding="utf-8")
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))

    def test_adoption_requires_existing_work_without_operator_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            observations = json.loads((receipts / "observations.json").read_text(encoding="utf-8"))
            observations["adopted_existing"] = False
            (receipts / "observations.json").write_text(json.dumps(observations), encoding="utf-8")
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))

    def test_default_merge_requires_captain_approval(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    merge={
                        "policy": "unattended",
                        "when_clean": True,
                        "directive": None,
                    }
                ),
                **_bind(),
            )
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    merge={
                        "policy": "unattended",
                        "when_clean": True,
                        "directive": "Merge when clean: yes",
                    }
                ),
                **_bind(),
            )

    def test_clean_review_must_cite_real_prior_repair(self) -> None:
        reviews = [
            _review(
                head=HEAD_A,
                outcome="changes_requested",
                reviewed_at="2026-09-21T16:00:00+00:00",
                review_id=1001,
            ),
            _review(
                head=HEAD_B,
                outcome="clean",
                reviewed_at="2026-09-21T16:20:00+00:00",
                review_id=1002,
                prior_repair="",
            ),
        ]
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())
        reviews[-1]["prior_repair"] = "manufactured defect"
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())
        reviews[-1]["prior_repair"] = "https://github.com/example/other/pull/1"
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())
        reviews[-1]["prior_repair"] = "https://github.com/example-org/demo-repo/pull/999999999"
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())

    def test_fabricated_prior_repair_without_live_transition_is_rejected(self) -> None:
        github = _trusted_github()
        github["reviews"] = [github["reviews"][-1]]
        github["prior_repairs"] = [
            {"html_url": "https://github.com/example-org/demo-repo/pull/999999999", "head": HEAD_A}
        ]
        clean_review = dict(
            _valid_payload()["reviews"][-1],
            prior_repair="https://github.com/example-org/demo-repo/pull/999999999",
        )
        with self.assertRaises(df.DogfoodError):
            df._validate_reviews_and_repairs(
                [clean_review],
                [],
                PR_URL,
                HEAD_B,
                _bound(),
                github,
            )
        live = _trusted_github()
        trusted = _trusted_github()
        trusted["prior_repairs"] = [
            {
                "html_url": "https://github.com/example-org/demo-repo/pull/999999999",
                "head": HEAD_C,
                "head_before": HEAD_A,
                "repaired_at": "2026-09-20T16:10:00+00:00",
            }
        ]
        with self.assertRaises(df.DogfoodError):
            df.github_evidence_matches(live, trusted)

    def test_fetch_live_github_binds_prior_repair_transition(self) -> None:
        ordered = _live_github_run_mocks()
        with mock.patch(
            "hermes_helmet.dogfood.subprocess.run",
            side_effect=ordered,
        ) as run:
            live = df.fetch_live_github(
                PR_URL,
                _bound(),
                prior_repairs=[{"html_url": OTHER_PR_URL}],
            )
        self.assertEqual(run.call_count, 6)
        self.assertEqual(
            live["prior_repairs"],
            [
                {
                    "html_url": OTHER_PR_URL,
                    "head": HEAD_C,
                    "head_before": HEAD_A,
                    "repaired_at": "2026-09-20T16:10:00+00:00",
                }
            ],
        )
        with mock.patch(
            "hermes_helmet.dogfood.subprocess.run",
            side_effect=_live_github_run_mocks(prior_ok=False),
        ):
            with self.assertRaises(df.DogfoodError):
                df.fetch_live_github(
                    PR_URL,
                    _bound(),
                    prior_repairs=[
                        {"html_url": "https://github.com/example-org/demo-repo/pull/999999999"}
                    ],
                )

    def test_fetch_live_prior_repair_rejects_review_submitted_after_head(self) -> None:
        late = _live_github_run_mocks(
            prior_reviews=[
                {
                    "id": 9001,
                    "user": {"login": "example-captain"},
                    "commit_id": HEAD_A,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-09-20T17:00:00+00:00",
                    "html_url": f"{OTHER_PR_URL}#pullrequestreview-9001",
                }
            ]
        )
        with mock.patch("hermes_helmet.dogfood.subprocess.run", side_effect=late):
            with self.assertRaises(df.DogfoodError):
                df.fetch_live_github(
                    PR_URL,
                    _bound(),
                    prior_repairs=[{"html_url": OTHER_PR_URL}],
                )

    def test_fetch_live_prior_repair_rejects_unrelated_reviewer(self) -> None:
        unrelated = _live_github_run_mocks(
            prior_reviews=[
                {
                    "id": 9001,
                    "user": {"login": "unrelated-account"},
                    "commit_id": HEAD_A,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-09-20T16:00:00+00:00",
                    "html_url": f"{OTHER_PR_URL}#pullrequestreview-9001",
                }
            ]
        )
        with mock.patch("hermes_helmet.dogfood.subprocess.run", side_effect=unrelated):
            with self.assertRaises(df.DogfoodError):
                df.fetch_live_github(
                    PR_URL,
                    _bound(),
                    prior_repairs=[{"html_url": OTHER_PR_URL}],
                )

    def test_clean_review_requires_validated_same_pr_repair_history(self) -> None:
        github = _trusted_github()
        clean_review = _valid_payload()["reviews"][-1]
        github["reviews"] = [github["reviews"][-1]]
        with self.assertRaises(df.DogfoodError):
            df._validate_reviews_and_repairs(
                [clean_review],
                [],
                PR_URL,
                HEAD_B,
                _bound(),
                github,
            )

    def test_clean_review_may_cite_verified_train_repair_without_same_pr_repair(self) -> None:
        github = _trusted_github()
        github["reviews"] = [github["reviews"][-1]]
        github["prior_repairs"] = [
            {
                "html_url": OTHER_PR_URL,
                "head": HEAD_C,
                "head_before": HEAD_A,
                "repaired_at": "2026-09-20T16:10:00+00:00",
            }
        ]
        clean_review = dict(_valid_payload()["reviews"][-1], prior_repair=OTHER_PR_URL)
        reviews, repairs, lapses = df._validate_reviews_and_repairs(
            [clean_review],
            [],
            PR_URL,
            HEAD_B,
            _bound(),
            github,
        )
        self.assertEqual(reviews[-1]["prior_repair"], OTHER_PR_URL)
        self.assertEqual(repairs, [])
        self.assertEqual(
            lapses,
            [{"sha": HEAD_A, "committed_at": "2026-09-21T15:50:00+00:00"}],
        )

    def test_intermediate_commit_is_late_review_lapse_not_required_head(self) -> None:
        github = _trusted_github()
        extra = {"sha": HEAD_C, "committed_at": "2026-09-21T16:05:00+00:00"}
        github["commits"] = list(github["commits"][:1]) + [extra] + list(github["commits"][1:])
        payload = _valid_payload()
        reviews, repairs, lapses = df._validate_reviews_and_repairs(
            payload["reviews"],
            payload["repairs"],
            PR_URL,
            HEAD_B,
            _bound(),
            github,
        )
        self.assertEqual([review["head"] for review in reviews], [HEAD_A, HEAD_B])
        self.assertEqual(repairs[0]["head_after"], HEAD_B)
        self.assertEqual(lapses, [extra])

    def test_reviews_must_bind_captain_review_ids_and_commits(self) -> None:
        reviews = _valid_payload()["reviews"]
        reviews[0] = dict(reviews[0], review_id=9999)
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())
        reviews = _valid_payload()["reviews"]
        reviews[0] = dict(reviews[0], reviewer="example-agent")
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(reviews=reviews), **_bind())
        repairs = _valid_payload()["repairs"]
        repairs[0] = dict(repairs[0], repaired_at="2026-09-21T16:11:00+00:00")
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(repairs=repairs), **_bind())
        payload = _valid_payload()
        payload["reviews"][0]["event_id"] = 2001
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(payload, **_bind())

    def test_missing_repair_for_head_transition_is_rejected(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(_valid_payload(repairs=[]), **_bind())

    def test_current_head_mismatch_is_rejected(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(current_head=HEAD_A), **_bind()
            )
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(), **_bind(expected_head=HEAD_A)
            )
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(published_pr=OTHER_PR_URL), **_bind()
            )

    def test_live_github_mismatch_is_rejected(self) -> None:
        live = _trusted_github()
        live["pull"] = dict(live["pull"])
        live["pull"]["head"] = dict(live["pull"]["head"], sha=HEAD_A)
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(), **_bind(live_github=live)
            )

    def test_non_evidence_and_secret_command_arguments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            _write_receipt(
                receipts,
                "doctor",
                command=["hermes-helmet", "doctor", "--help"],
            )
            with self.assertRaises(df.DogfoodError) as ctx:
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))
            self.assertNotIn(SECRET_VALUE, str(ctx.exception))
            _write_receipt(
                receipts,
                "doctor",
                command=["hermes-helmet", "doctor", "--token", SECRET_VALUE],
            )
            with self.assertRaises(df.DogfoodError) as ctx:
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))
            self.assertNotIn(SECRET_VALUE, str(ctx.exception))

    def test_fabricated_boolean_or_copied_receipts_are_rejected(self) -> None:
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    runtime={
                        "dogfood_host": "codex",
                        "claude_required": False,
                        "hermes_host_required": False,
                        "checks": {name: True for name in df.CODEX_CHECKS},
                    }
                ),
                **_bind(),
            )
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            _write_receipt(receipts, "doctor", output=CHECK_OUTPUTS["setup"])
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))
            _write_receipt(receipts, "setup", command=["echo", "ok"])
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))

    def test_unknown_secret_fields_are_rejected_without_echoing_values(self) -> None:
        payload = _valid_payload()
        payload["api_token"] = SECRET_VALUE
        with self.assertRaises(df.DogfoodError) as ctx:
            df.validate_runtime_evidence(payload, **_bind())
        self.assertNotIn(SECRET_VALUE, str(ctx.exception))
        nested = _valid_payload()
        nested["runtime"] = dict(nested["runtime"])
        nested["runtime"]["api_token"] = SECRET_VALUE
        with self.assertRaises(df.DogfoodError) as ctx:
            df.validate_runtime_evidence(nested, **_bind())
        self.assertNotIn(SECRET_VALUE, str(ctx.exception))
        reconstructed = df.validate_runtime_evidence(_valid_payload(), **_bind())
        self.assertEqual(
            set(reconstructed),
            {
                "version",
                "channel",
                "minted_version",
                "source_revision",
                "image",
                "task_id",
                "published_pr",
                "current_head",
                "branch",
                "worker",
                "adopted_existing",
                "operator_announcement",
                "merge",
                "reviews",
                "repairs",
                "late_review_lapses",
                "rollback",
                "runtime",
                "visibility",
                "checkpoint",
                "dispatch",
                "wrapper",
            },
        )

    def test_rollback_must_restore_authorized_digest_without_rebuild_or_mint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            observations = json.loads((receipts / "observations.json").read_text(encoding="utf-8"))
            observations["rollback"]["rebuilt"] = True
            (receipts / "observations.json").write_text(json.dumps(observations), encoding="utf-8")
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))

    def test_codex_checks_are_required_and_other_hosts_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = _copy_receipts(Path(tmp))
            _write_receipt(receipts, "default_integrations", exit_code=1)
            with self.assertRaises(df.DogfoodError):
                df.validate_runtime_evidence(_valid_payload(), **_bind(receipts_dir=receipts))
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    runtime={
                        "dogfood_host": "claude",
                        "claude_required": False,
                        "hermes_host_required": False,
                    }
                ),
                **_bind(),
            )
        with self.assertRaises(df.DogfoodError):
            df.validate_runtime_evidence(
                _valid_payload(
                    runtime={
                        "dogfood_host": "codex",
                        "claude_required": True,
                        "hermes_host_required": False,
                    }
                ),
                **_bind(),
            )


class CliAndVerifyTests(unittest.TestCase):
    def test_cli_validates_example_json(self) -> None:
        parser = helmet_cli.build_parser()
        args = parser.parse_args(
            [
                "dogfood-evidence",
                str(ROOT / "config" / "dogfood-evidence.example.json"),
                "--bound",
                str(BOUND_PATH),
                "--policy",
                str(POLICY_PATH),
                "--receipts",
                str(RECEIPTS_PATH),
                "--head",
                HEAD_B,
                "--json",
            ]
        )
        buffer = io.StringIO()
        with mock.patch("hermes_helmet.cli.fetch_live_github", return_value=_trusted_github()):
            with mock.patch("sys.stdout", buffer):
                self.assertEqual(args.func(args), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["image"]["digest"], EXAMPLE_DIGEST)
        self.assertFalse(payload["worker"]["merged"])
        self.assertEqual(payload["current_head"], HEAD_B)
        self.assertEqual(
            payload["runtime"]["checks"]["setup"]["command"],
            ["hermes-helmet", "setup"],
        )
        self.assertEqual(payload["runtime"]["checks"]["epic_resume"]["command"][1], "epic")

    def test_cli_rejects_current_head_mismatch(self) -> None:
        parser = helmet_cli.build_parser()
        args = parser.parse_args(
            [
                "dogfood-evidence",
                str(ROOT / "config" / "dogfood-evidence.example.json"),
                "--bound",
                str(BOUND_PATH),
                "--policy",
                str(POLICY_PATH),
                "--receipts",
                str(RECEIPTS_PATH),
                "--head",
                HEAD_A,
            ]
        )
        buffer = io.StringIO()
        with mock.patch("hermes_helmet.cli.fetch_live_github", return_value=_trusted_github()):
            with mock.patch("sys.stderr", buffer):
                self.assertEqual(args.func(args), 1)
        self.assertIn("dogfood-evidence failed", buffer.getvalue())
        self.assertNotIn(SECRET_VALUE, buffer.getvalue())

    def test_cli_rejects_invalid_evidence(self) -> None:
        parser = helmet_cli.build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(_valid_payload(version="0.1.0")), encoding="utf-8")
            args = parser.parse_args(
                [
                    "dogfood-evidence",
                    str(path),
                    "--bound",
                    str(BOUND_PATH),
                    "--policy",
                    str(POLICY_PATH),
                    "--receipts",
                    str(RECEIPTS_PATH),
                    "--head",
                    HEAD_B,
                ]
            )
            buffer = io.StringIO()
            with mock.patch("hermes_helmet.cli.fetch_live_github", return_value=_trusted_github()):
                with mock.patch("sys.stderr", buffer):
                    self.assertEqual(args.func(args), 1)
            self.assertIn("dogfood-evidence failed", buffer.getvalue())

    def test_cli_requires_live_github_matching_trusted_receipt(self) -> None:
        parser = helmet_cli.build_parser()
        args = parser.parse_args(
            [
                "dogfood-evidence",
                str(ROOT / "config" / "dogfood-evidence.example.json"),
                "--bound",
                str(BOUND_PATH),
                "--policy",
                str(POLICY_PATH),
                "--receipts",
                str(RECEIPTS_PATH),
                "--head",
                HEAD_B,
                "--json",
            ]
        )
        fake_pull = mock.Mock(returncode=0, stdout=json.dumps({
            "html_url": PR_URL,
            "number": 48,
            "head": {"sha": HEAD_B, "ref": "automation/demo-issue-9"},
        }), stderr="")
        fake_reviews = mock.Mock(returncode=0, stdout=json.dumps(_trusted_github()["reviews"]), stderr="")
        fake_commits = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "sha": item["sha"],
                        "commit": {"committer": {"date": item["committed_at"]}},
                    }
                    for item in _trusted_github()["commits"]
                ]
            ),
            stderr="",
        )
        buffer = io.StringIO()
        with mock.patch(
            "hermes_helmet.dogfood.subprocess.run",
            side_effect=[fake_pull, fake_reviews, fake_commits],
        ) as run:
            with mock.patch("sys.stdout", buffer):
                self.assertEqual(args.func(args), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0][0], "gh")
        self.assertIn("repos/example-org/demo-repo/pulls/48", run.call_args_list[0].args[0])
        self.assertIn("reviews", run.call_args_list[1].args[0][-1])
        self.assertIn("commits", run.call_args_list[2].args[0][-1])
        self.assertNotIn("events", run.call_args_list[2].args[0][-1])
        mismatched = mock.Mock(returncode=0, stdout=json.dumps({
            "html_url": PR_URL,
            "number": 48,
            "head": {"sha": HEAD_A, "ref": "automation/demo-issue-9"},
        }), stderr="")
        with mock.patch(
            "hermes_helmet.dogfood.subprocess.run",
            side_effect=[mismatched, fake_reviews, fake_commits],
        ):
            with mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(args.func(args), 1)
                self.assertIn("dogfood-evidence failed", err.getvalue())

    def test_cli_has_no_unauthenticated_github_receipt_bypass(self) -> None:
        parser = helmet_cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "dogfood-evidence",
                    str(ROOT / "config" / "dogfood-evidence.example.json"),
                    "--bound",
                    str(BOUND_PATH),
                    "--policy",
                    str(POLICY_PATH),
                    "--receipts",
                    str(RECEIPTS_PATH),
                    "--head",
                    HEAD_B,
                    "--github-receipt",
                    str(RECEIPTS_PATH / "github.json"),
                ]
            )

    def test_verify_requires_dogfood_contract_files(self) -> None:
        verify = _read("scripts/verify.sh")
        for name in (
            "docs/dogfood.md",
            "config/dogfood-evidence.example.json",
            "config/dogfood-bound.json",
            "config/dogfood-receipts/github.json",
            "config/dogfood-receipts/observations.json",
            "src/hermes_helmet/dogfood.py",
            "tests/test_dogfood.py",
        ):
            self.assertIn(name, verify)
        self.assertNotIn("config/dogfood-github.example.json", verify)


if __name__ == "__main__":
    unittest.main()
