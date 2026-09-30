#!/usr/bin/env python3
"""Named-request repository enrollment tests.

Covers the exact requested-repo enrollment seam, repeat/restart idempotency,
read-only nonmutation, review-only ceilings, owner/Captain/restriction
revalidation, clone/path conflict failures, atomic interrupted persistence,
concurrent same-repo purpose upgrades, and origin→slug transport
normalization through a contract-faithful adapter.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_helmet import repo_enroll as re_mod  # noqa: E402
from hermes_helmet.authority import (  # noqa: E402
    AuthorityError,
    policy_from_mapping,
)


FIXTURE_POLICY = ROOT / "config" / "fixtures" / "exampleco" / "policy.json"
CAPTAIN = "example-captain"
WORKER = "example-agent"
NEW_SLUG = "example-org/new-repo"
NEW_WORKTREE = "/opt/data/repos/new-repo"
RUNTIME = ("hermes", "worker-runtime")


def _policy_mapping(**overrides: object) -> dict[str, object]:
    raw = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw


def _policy(**overrides: object):
    return policy_from_mapping(_policy_mapping(**overrides))


class ContractTransport:
    """Contract-faithful fake of the worker-runtime ``prepare-repo`` seam.

    Mirrors the published transport ABI: ``prepare-repo <ref> --purpose
    work|review --json`` where ``<ref>`` must be an exact owner/name slug or a
    canonical issue/PR URL — arbitrary Git origin syntax is rejected. Success
    JSON carries ``slug``, ``worktree``, ``purpose``, ``created``.
    """

    def __init__(self, *, worktree: str = NEW_WORKTREE, fail: bool = False) -> None:
        self.worktree = worktree
        self.fail = fail
        self.payload: object = None  # full override of the response payload
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def run(self, command) -> str:
        with self._lock:
            self.calls.append([str(part) for part in command])
            time.sleep(0.01)  # widen concurrency windows in threaded tests
        if self.fail:
            raise RuntimeError("clone validation failed")
        idx = command.index("prepare-repo")
        ref = str(command[idx + 1])
        purpose = str(command[idx + 3])
        assert command[idx + 2] == "--purpose"
        assert command[idx + 4] == "--json"
        # Contract: exact slug or canonical issue/PR URL only.
        slug: str | None = None
        if re_mod.REPOSITORY_RE.fullmatch(ref):
            slug = ref
        else:
            from hermes_helmet.github_issue_poller import (
                ISSUE_URL_RE,
                PULL_REQUEST_URL_RE,
            )

            match = PULL_REQUEST_URL_RE.fullmatch(ref) or ISSUE_URL_RE.fullmatch(ref)
            if match is not None:
                slug = match.group("slug")
        if slug is None:
            raise AssertionError(f"transport received non-canonical ref: {ref}")
        if self.payload is not None:
            return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return json.dumps(
            {
                "slug": slug,
                "worktree": self.worktree,
                "purpose": purpose,
                "created": True,
            }
        )


def _receipt(slug: str, purposes=("work",), actor: str = CAPTAIN, worktree: str = NEW_WORKTREE):
    return re_mod.EnrollmentReceipt(
        version=re_mod.RECEIPT_VERSION,
        slug=slug,
        purposes=tuple(purposes),
        worktree=worktree,
        request_ref=slug,
        actor=actor,
        authorized_at="2026-09-30T00:00:00+00:00",
        created=True,
    )


class ParseRepoRefTests(unittest.TestCase):
    def test_accepts_slug_issue_pr_and_origin_forms(self) -> None:
        cases = {
            "example-org/new-repo": "example-org/new-repo",
            "https://github.com/example-org/new-repo": "example-org/new-repo",
            "https://github.com/example-org/new-repo.git": "example-org/new-repo",
            "https://github.com/example-org/new-repo/issues/12": "example-org/new-repo",
            "https://github.com/example-org/new-repo/pull/34": "example-org/new-repo",
            "ssh://git@github.com/example-org/new-repo": "example-org/new-repo",
            "ssh://git@github.com/example-org/new-repo.git": "example-org/new-repo",
            "git@github.com:example-org/new-repo": "example-org/new-repo",
            "git@github.com:example-org/new-repo.git": "example-org/new-repo",
        }
        for ref, slug in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(re_mod.parse_repo_ref(ref), slug)

    def test_rejects_credentials_and_non_canonical_forms(self) -> None:
        credential = "https://user:tok@github.com/example-org/new-repo.git"
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_ref_credentials"):
            re_mod.parse_repo_ref(credential)
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_ref_credentials"):
            re_mod.parse_repo_ref("git@github.com:example-org/new-repo/extra")
        cases = {
            "": "enroll_ref_empty",
            "   ": "enroll_ref_empty",
            "\x07example-org/new-repo": "enroll_ref_control_chars",
            "http://github.com/example-org/new-repo": "enroll_ref_scheme_not_allowed",
            "https://github.com/example-org/new-repo?tab=readme": "enroll_ref_not_canonical",
            "https://gitlab.com/example-org/new-repo": "enroll_ref_not_canonical",
            "please clone example-org/new-repo": "enroll_ref_invalid",
        }
        for ref, code in cases.items():
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(re_mod.EnrollmentError, code):
                    re_mod.parse_repo_ref(ref)


class StoreTests(unittest.TestCase):
    def test_save_load_round_trip_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
            path = store.save(_receipt(NEW_SLUG))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)
            loaded = store.load(NEW_SLUG)
            assert loaded is not None
            self.assertEqual(loaded.slug, NEW_SLUG)
            self.assertEqual(loaded.purposes, ("work",))
            # Case-insensitive slug lookup.
            self.assertIsNotNone(store.load("Example-Org/New-Repo"))

    def test_corrupt_receipt_treated_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
            store.directory.mkdir(parents=True, exist_ok=True)
            store.path_for(NEW_SLUG).write_text('{"slug": ', encoding="utf-8")
            self.assertIsNone(store.load(NEW_SLUG))
            self.assertEqual(store.all(), [])
            # Re-enrollment repairs the corrupt file.
            store.save(_receipt(NEW_SLUG))
            self.assertIsNotNone(store.load(NEW_SLUG))

    def test_recover_removes_torn_tmp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
            store.directory.mkdir(parents=True, exist_ok=True)
            torn = store.directory / "example-org+new-repo.json.4242.deadbeef.tmp"
            torn.write_text("{}", encoding="utf-8")
            self.assertEqual(store.recover(), 1)
            self.assertFalse(torn.exists())

    def test_recover_waits_for_live_writer_instead_of_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
            store.directory.mkdir(parents=True, exist_ok=True)
            acquired = threading.Event()
            tmp_path = store.directory / "example-org+new-repo.json.99.aaaa.tmp"

            def writer() -> None:
                with store.locked():
                    tmp_path.write_text("{}", encoding="utf-8")
                    acquired.set()
                    time.sleep(0.3)  # hold the lock like a live writer

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(acquired.wait(timeout=10))
            start = time.monotonic()
            # recover() must block behind the live writer's lock, then clean.
            removed = store.recover()
            elapsed = time.monotonic() - start
            thread.join(timeout=10)
            self.assertGreaterEqual(elapsed, 0.1)
            self.assertEqual(removed, 1)
            self.assertFalse(tmp_path.exists())

    def test_lookups_on_missing_store_never_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "enrollments"
            store = re_mod.EnrollmentStore(directory)
            self.assertIsNone(store.load(NEW_SLUG))
            self.assertEqual(store.all(), [])
            self.assertFalse(directory.exists())


class PrepareRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = re_mod.EnrollmentStore(Path(self._tmp.name) / "enrollments")
        self.policy = _policy()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_allowlisted_repo_is_noop(self) -> None:
        transport = ContractTransport()
        result = re_mod.prepare_repository(
            self.policy,
            "example-org/demo-repo",
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(result.status, "allowlisted")
        self.assertFalse(result.receipt.created)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.all(), [])

    def test_enrolls_exact_requested_repo_and_persists_receipt(self) -> None:
        transport = ContractTransport()
        result = re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(result.status, "enrolled")
        self.assertTrue(result.receipt.created)
        self.assertEqual(result.receipt.purposes, ("work",))
        self.assertEqual(result.receipt.actor, CAPTAIN)
        self.assertEqual(result.receipt.request_ref, NEW_SLUG)
        self.assertEqual(
            transport.calls[0],
            [*RUNTIME, "prepare-repo", NEW_SLUG, "--purpose", "work", "--json"],
        )
        loaded = self.store.load(NEW_SLUG)
        assert loaded is not None
        self.assertEqual(loaded.worktree, NEW_WORKTREE)
        public = result.to_public_dict()
        self.assertEqual(public["slug"], NEW_SLUG)
        self.assertEqual(public["worktree"], NEW_WORKTREE)
        self.assertEqual(public["purpose"], "work")
        self.assertTrue(public["created"])

    def test_repeat_request_revalidates_transport_and_reuses(self) -> None:
        transport = ContractTransport()
        first = re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(first.status, "enrolled")
        # Repeat: transport seam still runs (access/clone revalidation), but no
        # new authority is granted and exactly one receipt remains.
        transport.worktree = NEW_WORKTREE
        second = re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(second.status, "reused")
        self.assertEqual(len(transport.calls), 2)
        receipts = self.store.all()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].purposes, ("work",))
        self.assertEqual(
            [p for p in self.store.directory.iterdir() if p.name.endswith(".tmp")],
            [],
        )

    def test_repeat_after_clone_validation_failure_keeps_receipt(self) -> None:
        transport = ContractTransport()
        re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        before = self.store.load(NEW_SLUG)
        transport.fail = True
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_transport_failed"):
            re_mod.prepare_repository(
                self.policy,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        self.assertEqual(self.store.load(NEW_SLUG), before)
        # Recovery path: once the transport validates again, the repeat reuses.
        transport.fail = False
        result = re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(result.status, "reused")

    def test_second_purpose_extends_receipt(self) -> None:
        transport = ContractTransport()
        re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="review",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        second = re_mod.prepare_repository(
            self.policy,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(second.status, "enrolled")
        self.assertEqual(second.receipt.purposes, ("review", "work"))
        self.assertEqual(len(self.store.all()), 1)

    def test_owner_not_allowed_rejected(self) -> None:
        transport = ContractTransport()
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_owner_not_allowed"):
            re_mod.prepare_repository(
                self.policy,
                "other-org/repo",
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.all(), [])

    def test_captain_identity_required(self) -> None:
        transport = ContractTransport()
        for actor in (WORKER, "", "someone-else"):
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(
                    re_mod.EnrollmentError, "enroll_captain_identity_mismatch"
                ):
                    re_mod.prepare_repository(
                        self.policy,
                        NEW_SLUG,
                        purpose="work",
                        runner=transport,
                        worker_runtime=RUNTIME,
                        actor_login=actor,
                        store=self.store,
                    )
        self.assertEqual(transport.calls, [])

    def test_explicit_enrollment_disable_and_purpose_restriction(self) -> None:
        transport = ContractTransport()
        disabled = _policy(enrollment={"enabled": False})
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_disabled"):
            re_mod.prepare_repository(
                disabled,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        review_only = _policy(enrollment={"allowed_purposes": ["review"]})
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_purpose_not_allowed"):
            re_mod.prepare_repository(
                review_only,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        allowed = re_mod.prepare_repository(
            review_only,
            NEW_SLUG,
            purpose="review",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(allowed.status, "enrolled")
        self.assertEqual(allowed.receipt.purposes, ("review",))

    def test_static_allowlist_unaffected_by_enrollment_restrictions(self) -> None:
        # Static/no-op alternative to the new/enrolled rejections above: an
        # existing allowlisted repository keeps its documented no-op when
        # enrollment is disabled or its purposes narrowed, without a transport
        # call or a stored receipt.
        transport = ContractTransport()
        disabled = _policy(enrollment={"enabled": False})
        result = re_mod.prepare_repository(
            disabled,
            "example-org/demo-repo",
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(result.status, "allowlisted")
        self.assertFalse(result.receipt.created)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.all(), [])
        # A narrowed allowed_purposes must not block a purpose the static
        # policy already grants.
        review_only = _policy(enrollment={"allowed_purposes": ["review"]})
        narrowed = re_mod.prepare_repository(
            review_only,
            "example-org/demo-repo",
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        self.assertEqual(narrowed.status, "allowlisted")
        self.assertFalse(narrowed.receipt.created)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.all(), [])

    def test_missing_worker_runtime_rejected(self) -> None:
        transport = ContractTransport()
        with self.assertRaisesRegex(re_mod.EnrollmentError, "worker_runtime_unset"):
            re_mod.prepare_repository(
                self.policy,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=(),
                actor_login=CAPTAIN,
                store=self.store,
            )
        self.assertEqual(transport.calls, [])

    def test_transport_response_validation_fails_closed(self) -> None:
        cases = {
            "not json": ("not-json", "enroll_transport_invalid_json"),
            "slug mismatch": (
                {"slug": "example-org/other", "worktree": NEW_WORKTREE, "purpose": "work", "created": True},
                "enroll_transport_slug_mismatch",
            ),
            "invalid slug": (
                {"slug": "nope", "worktree": NEW_WORKTREE, "purpose": "work", "created": True},
                "enroll_transport_slug_invalid",
            ),
            "purpose mismatch": (
                {"slug": NEW_SLUG, "worktree": NEW_WORKTREE, "purpose": "review", "created": True},
                "enroll_transport_purpose_mismatch",
            ),
            "missing worktree": (
                {"slug": NEW_SLUG, "purpose": "work", "created": True},
                "enroll_transport_worktree_missing",
            ),
            "created not bool": (
                {"slug": NEW_SLUG, "worktree": NEW_WORKTREE, "purpose": "work", "created": "yes"},
                "enroll_transport_created_invalid",
            ),
        }
        for name, (payload, code) in cases.items():
            with self.subTest(case=name):
                transport = ContractTransport()
                transport.payload = payload
                with self.assertRaisesRegex(re_mod.EnrollmentError, code):
                    re_mod.prepare_repository(
                        self.policy,
                        NEW_SLUG,
                        purpose="work",
                        runner=transport,
                        worker_runtime=RUNTIME,
                        actor_login=CAPTAIN,
                        store=self.store,
                    )
                self.assertEqual(self.store.all(), [])

    def test_worktree_collision_and_safety_checks(self) -> None:
        # Collision with a static allowlisted worktree.
        transport = ContractTransport(worktree="/opt/data/repos/demo-repo")
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_worktree_collision"):
            re_mod.prepare_repository(
                self.policy,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        # Collision between two different enrolled repos.
        shared = "/opt/data/repos/shared-checkout"
        ok = ContractTransport(worktree=shared)
        re_mod.prepare_repository(
            self.policy,
            "example-org/repo-a",
            purpose="work",
            runner=ok,
            worker_runtime=RUNTIME,
            actor_login=CAPTAIN,
            store=self.store,
        )
        colliding = ContractTransport(worktree=shared)
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_worktree_collision"):
            re_mod.prepare_repository(
                self.policy,
                "example-org/repo-b",
                purpose="work",
                runner=colliding,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        # Unsafe system path. ``/proc`` is a real (non-symlink) directory on
        # Linux and absent on macOS, so the symlink guard cannot pre-empt the
        # unsafe-prefix check on either platform — unlike ``/etc``, which is a
        # symlink to ``/private/etc`` on macOS and legitimately trips the
        # symlink guard first (covered by test_symlinked_worktree_rejected).
        unsafe = ContractTransport(worktree="/proc/new-repo")
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_worktree_unsafe"):
            re_mod.prepare_repository(
                self.policy,
                NEW_SLUG,
                purpose="work",
                runner=unsafe,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )

    def test_symlinked_worktree_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real)
            transport = ContractTransport(worktree=str(link / "checkout"))
            with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_worktree_symlink"):
                re_mod.prepare_repository(
                    self.policy,
                    NEW_SLUG,
                    purpose="work",
                    runner=transport,
                    worker_runtime=RUNTIME,
                    actor_login=CAPTAIN,
                    store=self.store,
                )

    def test_origin_refs_normalize_to_slug_for_transport(self) -> None:
        """HTTPS .git, ssh:// and SCP SSH origins never reach the transport raw."""
        origins = (
            "https://github.com/example-org/new-repo.git",
            "ssh://git@github.com/example-org/new-repo.git",
            "git@github.com:example-org/new-repo.git",
        )
        for origin in origins:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as tmp:
                store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
                transport = ContractTransport()
                result = re_mod.prepare_repository(
                    self.policy,
                    origin,
                    purpose="review",
                    runner=transport,
                    worker_runtime=RUNTIME,
                    actor_login=CAPTAIN,
                    store=store,
                )
                # The contract-faithful adapter rejects non-canonical refs, so
                # success proves the origin was normalized to the exact slug.
                self.assertEqual(
                    transport.calls[0],
                    [*RUNTIME, "prepare-repo", NEW_SLUG, "--purpose", "review", "--json"],
                )
                self.assertEqual(result.receipt.slug, NEW_SLUG)
                # The sanitized original reference stays only in the audit receipt.
                self.assertEqual(result.receipt.request_ref, origin)


class RevalidationTests(unittest.TestCase):
    """Stored receipts must follow current authority, not their own history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = re_mod.EnrollmentStore(Path(self._tmp.name) / "enrollments")
        self.policy = _policy()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enroll(self, purposes=("work",)) -> None:
        self.store.save(_receipt(NEW_SLUG, purposes=purposes))

    def test_resolve_prefers_allowlist(self) -> None:
        self.store.save(_receipt("example-org/demo-repo", purposes=("review",)))
        repository, purpose = re_mod.resolve_repository(
            self.policy, self.store, "example-org/demo-repo"
        )
        assert repository is not None
        self.assertEqual(str(repository.worktree), "/opt/data/repos/demo-repo")
        self.assertEqual(purpose, "")

    def test_resolve_unknown_and_case_insensitive(self) -> None:
        repository, purpose = re_mod.resolve_repository(self.policy, self.store, NEW_SLUG)
        self.assertIsNone(repository)
        self.assertEqual(purpose, "")
        self._enroll()
        repository, purpose = re_mod.resolve_repository(
            self.policy, self.store, "Example-Org/New-Repo"
        )
        assert repository is not None
        self.assertEqual(repository.slug, NEW_SLUG)
        self.assertEqual(purpose, "work")

    def test_owner_narrowing_revokes_stale_receipt(self) -> None:
        self._enroll()
        narrowed = _policy(
            github_owners=["other-org"],
            repositories=[
                {"slug": "other-org/base-repo", "worktree": "/opt/data/repos/base-repo"}
            ],
        )
        repository, purpose = re_mod.resolve_repository(narrowed, self.store, NEW_SLUG)
        self.assertIsNone(repository)
        self.assertEqual(purpose, "")
        self.assertEqual(
            [r.slug for r in re_mod.effective_repositories(narrowed, self.store)],
            ["other-org/base-repo"],
        )
        transport = ContractTransport()
        with self.assertRaisesRegex(re_mod.EnrollmentError, "enroll_owner_not_allowed"):
            re_mod.prepare_repository(
                narrowed,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )

    def test_captain_change_revokes_stale_receipt(self) -> None:
        self._enroll()
        recaptained = _policy(captain_github_login="new-captain")
        repository, _ = re_mod.resolve_repository(recaptained, self.store, NEW_SLUG)
        self.assertIsNone(repository)
        # The new Captain can re-enroll explicitly; the stale actor is replaced.
        transport = ContractTransport()
        result = re_mod.prepare_repository(
            recaptained,
            NEW_SLUG,
            purpose="work",
            runner=transport,
            worker_runtime=RUNTIME,
            actor_login="new-captain",
            store=self.store,
        )
        self.assertEqual(result.status, "enrolled")
        self.assertEqual(result.receipt.actor, "new-captain")
        repository, purpose = re_mod.resolve_repository(recaptained, self.store, NEW_SLUG)
        self.assertIsNotNone(repository)
        self.assertEqual(purpose, "work")

    def test_enrollment_disable_revokes_lookup_but_not_allowlist(self) -> None:
        self._enroll()
        disabled = _policy(enrollment={"enabled": False})
        repository, _ = re_mod.resolve_repository(disabled, self.store, NEW_SLUG)
        self.assertIsNone(repository)
        # Static allowlist remains intact.
        repository, purpose = re_mod.resolve_repository(
            disabled, self.store, "example-org/demo-repo"
        )
        self.assertIsNotNone(repository)
        self.assertEqual(purpose, "")
        self.assertEqual(
            [r.slug for r in re_mod.effective_repositories(disabled, self.store)],
            ["example-org/demo-repo", "example-org/docs-repo"],
        )

    def test_purpose_narrowing_drops_stale_grants(self) -> None:
        self._enroll(purposes=("review", "work"))
        narrowed = _policy(enrollment={"allowed_purposes": ["review"]})
        repository, purpose = re_mod.resolve_repository(narrowed, self.store, NEW_SLUG)
        self.assertIsNotNone(repository)
        self.assertEqual(purpose, "review")

    def test_review_only_effective_purpose(self) -> None:
        self._enroll(purposes=("review",))
        _repository, purpose = re_mod.resolve_repository(self.policy, self.store, NEW_SLUG)
        self.assertEqual(purpose, "review")
        self.assertEqual(re_mod.effective_purpose(("review",)), "review")
        self.assertEqual(re_mod.effective_purpose(("review", "work")), "work")


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = re_mod.EnrollmentStore(Path(self._tmp.name) / "enrollments")
        self.policy = _policy()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, ref: str, purpose: str, transport, results: list, index: int) -> None:
        try:
            results[index] = re_mod.prepare_repository(
                self.policy,
                ref,
                purpose=purpose,
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=self.store,
            )
        except Exception as exc:  # noqa: BLE001 - surface in assertions
            results[index] = exc

    def test_concurrent_same_repo_purpose_upgrade(self) -> None:
        transport = ContractTransport()
        results: list[object] = [None, None]
        threads = [
            threading.Thread(
                target=self._run, args=(NEW_SLUG, "review", transport, results, 0)
            ),
            threading.Thread(
                target=self._run, args=(NEW_SLUG, "work", transport, results, 1)
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for result in results:
            self.assertIsInstance(result, re_mod.EnrollmentResult, msg=str(result))
        receipts = self.store.all()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(set(receipts[0].purposes), {"review", "work"})
        # Both requests re-ran the transport seam.
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            [p for p in self.store.directory.iterdir() if p.name.endswith(".tmp")],
            [],
        )

    def test_concurrent_different_repos(self) -> None:
        results: list[object] = [None, None]
        transport_a = ContractTransport(worktree="/opt/data/repos/repo-a")
        transport_b = ContractTransport(worktree="/opt/data/repos/repo-b")
        threads = [
            threading.Thread(
                target=self._run,
                args=("example-org/repo-a", "work", transport_a, results, 0),
            ),
            threading.Thread(
                target=self._run,
                args=("example-org/repo-b", "review", transport_b, results, 1),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for result in results:
            self.assertIsInstance(result, re_mod.EnrollmentResult, msg=str(result))
        receipts = {receipt.slug for receipt in self.store.all()}
        self.assertEqual(receipts, {"example-org/repo-a", "example-org/repo-b"})


class InterruptedEnrollmentTests(unittest.TestCase):
    def test_restart_after_torn_write_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = re_mod.EnrollmentStore(Path(tmp) / "enrollments")
            store.directory.mkdir(parents=True, exist_ok=True)
            # Simulate an interrupted prior write.
            torn = store.directory / "example-org+new-repo.json.7777.beefbeef.tmp"
            torn.write_text("{}", encoding="utf-8")
            policy = _policy()
            transport = ContractTransport()
            result = re_mod.prepare_repository(
                policy,
                NEW_SLUG,
                purpose="work",
                runner=transport,
                worker_runtime=RUNTIME,
                actor_login=CAPTAIN,
                store=store,
            )
            self.assertEqual(result.status, "enrolled")
            self.assertFalse(torn.exists())
            self.assertIsNotNone(store.load(NEW_SLUG))
            self.assertEqual(
                [p for p in store.directory.iterdir() if p.name.endswith(".tmp")],
                [],
            )


class PolicySchemaTests(unittest.TestCase):
    def test_enrollment_defaults_and_round_trip(self) -> None:
        from hermes_helmet import authority

        policy = _policy()
        self.assertTrue(policy.enrollment.enabled)
        self.assertEqual(
            set(policy.enrollment.allowed_purposes), {"work", "review"}
        )
        restored = authority.policy_from_mapping(authority.authority_public_dict(policy))
        self.assertEqual(restored.enrollment, policy.enrollment)

    def test_enrollment_nondefault_survives_canonical_serialization(self) -> None:
        """Doctor/setup canonicalize via authority_public_dict; explicit
        enrollment ceilings must not regress to defaults through that path."""
        from hermes_helmet import authority

        for enrollment_cfg in (
            {"enabled": False},
            {"allowed_purposes": ["review"]},
            {"enabled": True, "allowed_purposes": ["work"]},
        ):
            with self.subTest(enrollment=enrollment_cfg):
                policy = _policy(enrollment=enrollment_cfg)
                canonical = json.dumps(
                    authority.authority_public_dict(policy), indent=2, sort_keys=True
                )
                self.assertIn("enrollment", canonical)
                restored = authority.policy_from_mapping(json.loads(canonical))
                self.assertEqual(restored.enrollment, policy.enrollment)
        disabled = _policy(enrollment={"enabled": False})
        self.assertFalse(
            authority.policy_from_mapping(
                authority.authority_public_dict(disabled)
            ).enrollment.enabled
        )

    def test_enrollment_validation(self) -> None:
        with self.assertRaisesRegex(AuthorityError, "enrollment"):
            _policy(enrollment="yes")
        with self.assertRaisesRegex(AuthorityError, "enrollment.enabled"):
            _policy(enrollment={"enabled": "yes"})
        with self.assertRaisesRegex(AuthorityError, "enrollment.allowed_purposes"):
            _policy(enrollment={"allowed_purposes": ["merge"]})
        with self.assertRaisesRegex(AuthorityError, "enrollment.allowed_purposes"):
            _policy(enrollment={"allowed_purposes": []})
        with self.assertRaisesRegex(AuthorityError, "enrollment.bogus"):
            _policy(enrollment={"bogus": True})


if __name__ == "__main__":
    unittest.main()
