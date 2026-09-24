#!/usr/bin/env python3
"""H12 private v0.1.0 release-candidate contract tests."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from hermes_helmet import release_candidate as rc


ROOT = Path(__file__).resolve().parents[1]
H11_SOURCE = "2c687a453fffe06385e2ab858288e23a153cfb10"
H11_DIGEST = "sha256:316b6849cf6d663dc7fe40d7b3a543ae1f0d16d20b9dd58e0f0b2564c8424691"
PINNED_BASE = (
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
PINNED_BASE_REF = f"nousresearch/hermes-agent:v2026.9.14@{PINNED_BASE}"
DECISION_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _sanitized_report(
    *,
    platform: str,
    inherited: int,
    expected_base_ref: str = PINNED_BASE_REF,
    added: list[object] | None = None,
    secrets: int = 0,
    failures: list[str] | None = None,
    trivy_version: str = "0.74.0",
) -> dict[str, object]:
    return {
        "policy": {
            "blocking_severities": ["CRITICAL", "HIGH"],
            "wrapper_added_findings_allowed": 0,
            "secret_findings_allowed": 0,
        },
        "platform": platform,
        "expected_base_ref": expected_base_ref,
        "trivy_version": trivy_version,
        "trivy_database_sha256": "d" * 64,
        "trivy_java_database_sha256": "absent",
        "base": {
            "artifact_id": "sha256:base",
            "image_id": "sha256:base",
            "high_or_critical": inherited,
            "layers": 1,
        },
        "derived": {
            "artifact_id": "sha256:derived",
            "image_id": "sha256:derived",
            "high_or_critical": inherited + len(added or []),
            "layers": 2,
            "secret_findings": secrets,
        },
        "delta": {
            "added_high_or_critical": list(added or []),
            "removed_high_or_critical": [],
        },
        "failures": list(failures or []),
    }


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _complete_artifacts() -> tuple[rc.Artifact, ...]:
    names = [
        "hermes_helmet-0.1.0rc1-py3-none-any.whl",
        "hermes_helmet-0.1.0rc1.tar.gz",
        *rc.REQUIRED_DOCUMENT_FILES,
        *rc.REQUIRED_REPORT_FILES,
    ]
    return tuple(rc.Artifact(name, f"{index:064x}") for index, name in enumerate(names, start=1))


def _write_complete_dist(directory: Path, *, digest: str, revision: str) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "hermes_helmet-0.1.0rc1-py3-none-any.whl": b"wheel",
        "hermes_helmet-0.1.0rc1.tar.gz": b"sdist",
        "CHANGELOG.md": b"changelog",
        "LICENSE": b"license",
        "NOTICE": b"notice",
        "source-sbom.spdx.json": b"{}",
        "delta-amd64.json": json.dumps(
            _sanitized_report(platform="linux/amd64", inherited=446)
        ).encode("utf-8"),
        "delta-arm64.json": json.dumps(
            _sanitized_report(platform="linux/arm64", inherited=528)
        ).encode("utf-8"),
    }
    for name, body in files.items():
        (directory / name).write_bytes(body)
    rc.write_provenance_file(directory, source_revision=revision, image_digest=digest)
    artifacts = rc.collect_artifacts(directory, complete=True)
    rc.write_checksums(directory, artifacts)
    payload = rc.build_evidence(
        source_revision=revision,
        image_digest=digest,
        artifacts=artifacts,
    )
    rc.write_evidence_file(directory / "release-evidence.json", payload)
    return payload


def _absent_github_ref(tag: str, *, expected_revision: str, **_: object) -> rc.GitHubCandidateRef:
    del expected_revision
    return rc.GitHubCandidateRef(
        tag=tag,
        commit=None,
        tag_present=False,
        release_present=False,
    )


def _release_asset_names(dist: Path) -> list[str]:
    names = [item.name for item in rc.collect_artifacts(dist, complete=True)]
    for extra in rc.GITHUB_RELEASE_EXTRA_ASSETS:
        if extra not in names:
            names.append(extra)
    return names


def _matching_github_assets(
    dist: Path,
    *,
    empty: bool = False,
    omit: str | None = None,
    wrong: str | None = None,
) -> tuple[rc.GitHubReleaseAsset, ...]:
    if empty:
        return ()
    assets: list[rc.GitHubReleaseAsset] = []
    for name in _release_asset_names(dist):
        if name == omit:
            continue
        digest = f"sha256:{rc.sha256_file(dist / name)}"
        if name == wrong:
            digest = "sha256:" + ("ff" * 32)
        assets.append(rc.GitHubReleaseAsset(name=name, digest=digest))
    return tuple(assets)


def _reuse_github_ref(
    dist: Path,
    *,
    empty: bool = False,
    omit: str | None = None,
    wrong: str | None = None,
):
    assets = _matching_github_assets(dist, empty=empty, omit=omit, wrong=wrong)

    def inspect(tag: str, *, expected_revision: str, **_: object) -> rc.GitHubCandidateRef:
        return rc.GitHubCandidateRef(
            tag=tag,
            commit=expected_revision,
            tag_present=True,
            release_present=True,
            prerelease=True,
            reuse_existing=True,
            assets=assets,
        )

    return inspect


def _workflow() -> str:
    return _read(".github/workflows/verify.yml")


def _all_workflows() -> str:
    texts = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        texts.append(path.read_text(encoding="utf-8"))
    return "\n".join(texts)


class VersionAndDocsTests(unittest.TestCase):
    def test_package_version_is_private_rc_not_minted_release(self) -> None:
        pyproject = _read("pyproject.toml")
        self.assertIn('version = "0.1.0rc1"', pyproject)
        self.assertNotIn('version = "0.0.0.dev0"', pyproject)
        self.assertEqual(rc.PACKAGE_VERSION, "0.1.0rc1")
        self.assertEqual(rc.IMAGE_VERSION, "0.1.0rc1")
        self.assertEqual(rc.CHANNEL, "private-rc")
        self.assertEqual(rc.MINTED_VERSION, "0.1.0")
        init = _read("src/hermes_helmet/__init__.py")
        self.assertIn('__version__ = "0.1.0rc1"', init)

    def test_changelog_records_private_rc_without_claiming_public_release(self) -> None:
        changelog = _read("CHANGELOG.md")
        self.assertIn("## 0.1.0rc1", changelog)
        self.assertIn("private", changelog.casefold())
        self.assertIn("release candidate", changelog.casefold())
        self.assertNotRegex(
            changelog,
            r"(?i)public (v)?0\.1\.0",
        )
        header = changelog.split("## 0.1.0rc1", 1)[1].split("\n## ", 1)[0].casefold()
        self.assertIn("setup-helmet", header)
        self.assertIn("helmet-issue", header)
        self.assertIn("helmet-epic", header)

    def test_release_candidate_doc_records_evidence_rollback_and_owner_gate(self) -> None:
        docs = _read("docs/release-candidate.md")
        self.assertIn("linux/amd64", docs)
        self.assertIn("linux/arm64", docs)
        self.assertIn("immutable", docs.casefold())
        self.assertIn("public-preview.md", docs)
        self.assertIn("currently running immutable", docs)
        self.assertIn("rollback", docs.casefold())
        self.assertIn("owner decision", docs.casefold())
        self.assertIn("without rebuild", docs.casefold())
        self.assertIn("private", docs.casefold())
        for skill in rc.BUNDLED_SKILLS:
            self.assertIn(skill, docs)
        self.assertIn("helmet setup", docs)
        self.assertIn("helmet doctor", docs)
        self.assertIn("helmet status", docs)

    def test_security_policy_requires_named_owner_decision_before_minting(self) -> None:
        policy = " ".join(_read("SECURITY.md").split())
        self.assertIn("exact base digest", policy)
        self.assertIn("both platform", policy)
        self.assertIn("inherited", policy.casefold())
        self.assertIn("expires", policy.casefold())
        self.assertIn("owner decision", policy)
        self.assertIn("without rebuild", policy)
        self.assertIn("dev-<source-commit>", policy)
        self.assertIn("not a numbered release", policy)


class EvidenceSchemaTests(unittest.TestCase):
    def _valid_payload(self) -> dict[str, object]:
        digest = "sha256:" + ("ab" * 32)
        return {
            "version": "0.1.0rc1",
            "channel": "private-rc",
            "minted_version": "0.1.0",
            "source_revision": "a" * 40,
            "base_digest": PINNED_BASE,
            "image": {
                "repository": "ghcr.io/machinewisdomai/hermes-helmet-oss",
                "digest": digest,
                "platforms": ["linux/amd64", "linux/arm64"],
                "tags": [f"dev-{'a' * 40}"],
                "version": "0.1.0rc1",
            },
            "artifacts": [
                {"name": item.name, "sha256": item.sha256} for item in _complete_artifacts()
            ],
            "bundled_skills": list(rc.BUNDLED_SKILLS),
            "documents": {
                "changelog": "CHANGELOG.md",
                "license": "LICENSE",
                "notice": "NOTICE",
                "sbom": "source-sbom.spdx.json",
                "provenance": "provenance.json",
            },
            "rollback": {
                "previous_source_revision": H11_SOURCE,
                "previous_image_ref": (
                    "ghcr.io/machinewisdomai/hermes-helmet-oss:dev-" + H11_SOURCE
                ),
                "previous_image_digest": H11_DIGEST,
                "previous_platforms": ["linux/amd64", "linux/arm64"],
                "procedure": rc.ROLLBACK_PROCEDURE,
            },
            "visibility": {
                "repository": "private",
                "release": "private",
                "package": "private",
                "image": "private",
                "publication_change": False,
            },
            "promotion": {
                "automatic": False,
                "requires_owner_decision": True,
                "rebuild_forbidden": True,
            },
        }

    def test_valid_evidence_is_accepted(self) -> None:
        payload = self._valid_payload()
        self.assertEqual(rc.validate_evidence(payload), payload)

    def test_evidence_requires_one_digest_exact_revision_and_checksums(self) -> None:
        payload = self._valid_payload()
        image = cast(dict[str, Any], dict(cast(dict[str, Any], payload["image"])))
        image["digest"] = "sha256:" + ("ab" * 32) + ",sha256:" + ("cd" * 32)
        payload["image"] = image
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "one immutable"):
            rc.validate_evidence(payload)

        payload = self._valid_payload()
        payload["source_revision"] = "short"
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "source revision"):
            rc.validate_evidence(payload)

        payload = self._valid_payload()
        payload["artifacts"] = [{"name": "wheel", "sha256": "nope"}]
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "checksum"):
            rc.validate_evidence(payload)

    def test_evidence_rejects_public_visibility_or_publication_change(self) -> None:
        payload = self._valid_payload()
        visibility = cast(dict[str, Any], dict(cast(dict[str, Any], payload["visibility"])))
        visibility["image"] = "public"
        payload["visibility"] = visibility
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "private"):
            rc.validate_evidence(payload)

        payload = self._valid_payload()
        visibility = cast(dict[str, Any], dict(cast(dict[str, Any], payload["visibility"])))
        visibility["publication_change"] = True
        payload["visibility"] = visibility
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "publication"):
            rc.validate_evidence(payload)

    def test_build_evidence_records_h11_rollback_target(self) -> None:
        built = rc.build_evidence(
            source_revision="b" * 40,
            image_digest="sha256:" + ("11" * 32),
            artifacts=_complete_artifacts(),
        )
        rc.validate_evidence(built)
        self.assertEqual(built["rollback"]["previous_source_revision"], H11_SOURCE)
        self.assertEqual(built["rollback"]["previous_image_digest"], H11_DIGEST)
        self.assertIn(H11_DIGEST, built["rollback"]["procedure"])
        self.assertEqual(built["rollback"]["previous_platforms"], ["linux/amd64", "linux/arm64"])
        self.assertEqual(built["base_digest"], PINNED_BASE)
        self.assertFalse(built["promotion"]["automatic"])
        self.assertTrue(built["promotion"]["rebuild_forbidden"])
        self.assertEqual(built["image"]["platforms"], ["linux/amd64", "linux/arm64"])
        self.assertEqual(built["image"]["version"], "0.1.0rc1")
        self.assertEqual(built["bundled_skills"], list(rc.BUNDLED_SKILLS))

    def test_evidence_rejects_development_or_numbered_image_version(self) -> None:
        payload = self._valid_payload()
        image = cast(dict[str, Any], dict(cast(dict[str, Any], payload["image"])))
        image["version"] = "0.0.0-dev"
        payload["image"] = image
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "image version"):
            rc.validate_evidence(payload)
        payload = self._valid_payload()
        image = cast(dict[str, Any], dict(cast(dict[str, Any], payload["image"])))
        image["version"] = "0.1.0"
        payload["image"] = image
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "image version"):
            rc.validate_evidence(payload)


class WorkflowPrivacyTests(unittest.TestCase):
    def test_ci_does_not_mint_numbered_v010_tag_or_github_release(self) -> None:
        combined = _all_workflows()
        self.assertNotIn("gh release create", combined)
        self.assertNotIn("softprops/action-gh-release@", combined)
        self.assertNotIn("actions/create-release@", combined)
        self.assertNotRegex(
            combined,
            r"ghcr\.io/machinewisdomai/hermes-helmet:0\.1\.0(?:-rc)?\b",
        )

    def test_ci_never_changes_visibility_to_public(self) -> None:
        combined = _all_workflows()
        self.assertNotRegex(combined, r"(?i)visibility\s*[:=]\s*public")
        self.assertNotIn("gh repo edit", combined)
        self.assertNotIn("change-package-visibility", combined)
        self.assertNotIn("/visibility", combined)
        self.assertIn('test "$visibility" = private', combined)

    def test_ci_packages_wheel_sdist_checksums_and_documents(self) -> None:
        workflow = _workflow()
        self.assertIn("scripts/package-release-candidate.sh", workflow)
        self.assertIn("scripts/prove-packaged-release.sh", workflow)
        self.assertIn("release-evidence.json", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("CHANGELOG.md", workflow)
        self.assertIn("LICENSE", workflow)
        self.assertIn("NOTICE", workflow)
        self.assertIn("source-sbom", workflow)
        self.assertIn("delta-amd64.json", workflow)
        self.assertIn("provenance.json", workflow)
        self.assertIn("ref: ${{ env.SOURCE_COMMIT }}", workflow)
        job = workflow[workflow.index("  release-candidate:") : workflow.index("  dev-image:")]
        self.assertIn("ref: ${{ env.SOURCE_COMMIT }}", job)
        self.assertLess(
            job.index("ref: ${{ env.SOURCE_COMMIT }}"),
            job.index("scripts/package-release-candidate.sh"),
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', job)

    def test_dev_image_checks_sha256sums_inside_dist_rc(self) -> None:
        workflow = _workflow()
        job = workflow[workflow.index("  dev-image:") :]
        self.assertNotIn("sha256sum --check dist/rc/SHA256SUMS", job)
        step = job[job.index("Verify packaged checksums") :]
        self.assertIn("working-directory: dist/rc", step)
        self.assertIn("sha256sum --check SHA256SUMS", step)
        self.assertLess(
            job.index("Record private release evidence"),
            job.index("Verify packaged checksums"),
        )

    def test_packaged_proof_runs_before_image_publication(self) -> None:
        workflow = _workflow()
        package = workflow.index("scripts/package-release-candidate.sh")
        prove = workflow.index("scripts/prove-packaged-release.sh")
        publication = workflow.index("push: true")
        self.assertLess(package, prove)
        self.assertLess(prove, publication)

    def test_scan_and_final_images_use_candidate_version(self) -> None:
        workflow = _workflow()
        self.assertNotIn("HERMES_HELMET_VERSION=0.0.0-dev", workflow)
        self.assertGreaterEqual(workflow.count("HERMES_HELMET_VERSION=0.1.0rc1"), 3)
        self.assertIn('"version": "0.1.0rc1"', workflow)
        smoke = _read("scripts/smoke-dev-image.sh")
        self.assertIn("org.opencontainers.image.version", smoke)
        self.assertIn("0.1.0rc1", smoke)

    def test_promotion_reuses_scanned_digest_and_is_not_invoked_by_ci(self) -> None:
        combined = _all_workflows()
        self.assertNotIn("scripts/promote-release-candidate.sh", combined)
        script = _read("scripts/promote-release-candidate.sh")
        self.assertIn("imagetools create", script)
        self.assertIn("gh release create", script)
        self.assertIn("--dry-run", script + _read("src/hermes_helmet/release_candidate.py"))
        self.assertNotRegex(script, r"\bdocker build\b")
        self.assertIn("HERMES_HELMET_OWNER_DECISION", script)
        self.assertIn("private", script)
        self.assertIn("HERMES_HELMET_PROMOTE", script)
        self.assertNotIn("gh release create", combined)
        self.assertNotIn('eval "$RELEASE_COMMAND"', script)
        self.assertIn("eval \"$COMMAND\"", script)


class OwnerDecisionAndPromotionTests(unittest.TestCase):
    def _decision(self) -> dict[str, object]:
        return {
            "version": "0.1.0rc1",
            "source_revision": "a" * 40,
            "base_digest": (
                "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
            ),
            "image_digest": "sha256:" + ("ab" * 32),
            "rollback_digest": H11_DIGEST,
            "platforms": {
                "linux/amd64": {
                    "report": "delta-amd64.json",
                    "report_sha256": "b" * 64,
                    "inherited_high_critical": 446,
                },
                "linux/arm64": {
                    "report": "delta-arm64.json",
                    "report_sha256": "c" * 64,
                    "inherited_high_critical": 528,
                },
            },
            "expires_at": "2026-10-20T00:00:00+00:00",
            "review_conditions": {
                "triggers": [
                    "next-stable-hermes",
                    "base-digest-change",
                    "emergency-security",
                ],
                "notes": "Re-review on the next stable Hermes release, a base-digest change, or an emergency security trigger.",
            },
            "accepted_inherited_risk": True,
            "publication": False,
            "visibility": "private",
            "evidence_sha256": "e" * 64,
        }

    def test_owner_decision_requires_named_fields(self) -> None:
        rc.validate_owner_decision(self._decision(), now=DECISION_NOW)
        incomplete = self._decision()
        del incomplete["expires_at"]
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "expires"):
            rc.validate_owner_decision(incomplete, now=DECISION_NOW)
        public = self._decision()
        public["publication"] = True
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "publication"):
            rc.validate_owner_decision(public, now=DECISION_NOW)

    def test_owner_decision_rejects_invalid_or_expired_timestamp(self) -> None:
        garbage = self._decision()
        garbage["expires_at"] = "whenever"
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "ISO-8601"):
            rc.validate_owner_decision(garbage, now=DECISION_NOW)
        past = self._decision()
        past["expires_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "expired"):
            rc.validate_owner_decision(past, now=DECISION_NOW)

    def test_owner_decision_rejects_expiry_beyond_30_days(self) -> None:
        far = self._decision()
        far["expires_at"] = "2028-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "30 days"):
            rc.validate_owner_decision(far, now=DECISION_NOW)
        boundary = self._decision()
        boundary["expires_at"] = "2026-10-20T00:00:00+00:00"
        rc.validate_owner_decision(boundary, now=DECISION_NOW)

    def test_owner_decision_requires_canonical_review_triggers(self) -> None:
        rc.validate_owner_decision(self._decision(), now=DECISION_NOW)
        freeform = self._decision()
        freeform["review_conditions"] = "x"
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "review conditions"):
            rc.validate_owner_decision(freeform, now=DECISION_NOW)
        incomplete = self._decision()
        incomplete["review_conditions"] = {
            "triggers": ["next-stable-hermes"],
            "notes": "missing required triggers",
        }
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "review conditions"):
            rc.validate_owner_decision(incomplete, now=DECISION_NOW)
        unknown = self._decision()
        unknown["review_conditions"] = {
            "triggers": [
                "next-stable-hermes",
                "base-digest-change",
                "emergency-security",
                "anything-else",
            ]
        }
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "review conditions"):
            rc.validate_owner_decision(unknown, now=DECISION_NOW)

    def test_sanitized_report_requires_trivy_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delta-amd64.json"
            payload = _sanitized_report(platform="linux/amd64", inherited=1)
            del payload["trivy_version"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "Trivy version"):
                rc.validate_sanitized_report(path, platform="linux/amd64")
            payload["trivy_version"] = ""
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "Trivy version"):
                rc.validate_sanitized_report(path, platform="linux/amd64")

    def test_promotion_fail_closes_without_owner_decision(self) -> None:
        with self.assertRaisesRegex(rc.ReleaseCandidateError, "owner decision"):
            rc.promotion_plan(decision=None, image_digest="sha256:" + ("ab" * 32))

    def test_promotion_reuses_existing_digest_and_forbids_rebuild(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            inspected: list[str] = []
            plan = rc.promotion_plan(
                decision=decision,
                image_digest=digest,
                evidence=evidence,
                dist_dir=dist,
                now=DECISION_NOW,
                inspect_rollback=inspected.append,
            )
            self.assertEqual(plan.source_digest, digest)
            self.assertEqual(plan.tags, ("0.1.0rc1",))
            self.assertNotIn("0.1.0", plan.tags)
            self.assertTrue(plan.reuse_existing_manifest)
            self.assertFalse(plan.rebuild)
            self.assertEqual(plan.rollback_digest, H11_DIGEST)
            self.assertEqual(inspected, [H11_DIGEST])
            argv = rc.promotion_command(plan)
            self.assertIn("imagetools", argv)
            self.assertIn("create", argv)
            self.assertIn(digest, " ".join(argv))
            self.assertNotIn("build", argv)
            self.assertEqual(rc.rollback_command()[-1], rc.PREVIOUS_IMAGE_PIN)

    def test_promotion_fail_closes_on_cross_platform_trivy_version_mismatch(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            (dist / "delta-arm64.json").write_text(
                json.dumps(
                    _sanitized_report(
                        platform="linux/arm64",
                        inherited=528,
                        trivy_version="0.75.0",
                    )
                ),
                encoding="utf-8",
            )
            artifacts = rc.collect_artifacts(dist, complete=True)
            rc.write_checksums(dist, artifacts)
            evidence = rc.build_evidence(
                source_revision=revision, image_digest=digest, artifacts=artifacts
            )
            rc.write_evidence_file(dist / "release-evidence.json", evidence)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "Trivy version"):
                rc.promotion_plan(
                    decision=decision,
                    image_digest=digest,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_rollback=lambda digest: None,
                )

    def test_github_release_plan_is_private_prerelease_for_candidate_version(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            plan = rc.github_release_plan(
                decision=decision,
                evidence=evidence,
                dist_dir=dist,
                now=DECISION_NOW,
                dry_run=True,
                inspect_ref=_absent_github_ref,
            )
            self.assertEqual(plan.tag, "v0.1.0rc1")
            self.assertEqual(plan.target, revision)
            self.assertTrue(plan.prerelease)
            self.assertTrue(plan.dry_run)
            self.assertFalse(plan.latest)
            for name in (
                "hermes_helmet-0.1.0rc1-py3-none-any.whl",
                "hermes_helmet-0.1.0rc1.tar.gz",
                "SHA256SUMS",
                "CHANGELOG.md",
                "LICENSE",
                "NOTICE",
                "source-sbom.spdx.json",
                "provenance.json",
                "delta-amd64.json",
                "delta-arm64.json",
                "release-evidence.json",
            ):
                self.assertIn(name, plan.assets)
                self.assertTrue((dist / name).is_file())
            argv = rc.github_release_command(plan, dist_dir=dist)
            tag_argv = rc.github_tag_ref_command(plan)
            joined = " ".join(argv)
            self.assertEqual(plan.repository, "MachineWisdomAI/hermes-helmet")
            self.assertIn("gh", argv)
            self.assertIn("release", argv)
            self.assertIn("create", argv)
            self.assertIn("v0.1.0rc1", argv)
            self.assertIn("--prerelease", argv)
            self.assertIn("--repo", argv)
            self.assertIn("MachineWisdomAI/hermes-helmet", argv)
            self.assertIn("--verify-tag", argv)
            self.assertNotIn("--target", argv)
            self.assertIn(f"sha={revision}", tag_argv)
            self.assertIn(f"/repos/{plan.repository}/git/refs", tag_argv)
            self.assertIn("release-evidence.json", joined)
            self.assertNotIn("--draft", argv)
            quoted = rc.format_github_release_command(argv)
            self.assertIn("(private)", joined)
            plain_probe = subprocess.run(
                ["bash", "-n", "-c", joined],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(plain_probe.returncode, 0)
            quoted_probe = subprocess.run(
                ["bash", "-n", "-c", quoted],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(quoted_probe.returncode, 0)
            self.assertEqual(quoted, shlex.join(argv))

    def test_github_release_plan_rejects_tampered_checksum_manifest(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            (dist / "SHA256SUMS").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "checksum manifest"):
                rc.github_release_plan(
                    decision=decision,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_ref=_absent_github_ref,
                )

    def test_github_release_plan_fail_closes_on_mismatched_existing_tag(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40

        def _wrong_tag(tag: str, *, expected_revision: str, **_: object) -> rc.GitHubCandidateRef:
            del tag, expected_revision
            raise rc.ReleaseCandidateError(
                "existing candidate tag does not match the approved source revision"
            )

        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "approved source revision"):
                rc.github_release_plan(
                    decision=decision,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_ref=_wrong_tag,
                )

    def test_inspect_github_candidate_ref_reuses_matching_prerelease(self) -> None:
        revision = "a" * 40
        tag = "v0.1.0rc1"

        def api(path: str) -> tuple[int, dict[str, object] | None]:
            if path.endswith(f"/git/ref/tags/{tag}"):
                return 200, {"object": {"type": "commit", "sha": revision}}
            if path.endswith(f"/releases/tags/{tag}"):
                return 200, {
                    "prerelease": True,
                    "tag_name": tag,
                    "target_commitish": revision,
                }
            if path.endswith("/releases/latest"):
                return 404, {"message": "Not Found"}
            return 404, {"message": "Not Found"}

        state = rc.inspect_github_candidate_ref(
            tag, expected_revision=revision, api=api
        )
        self.assertTrue(state.reuse_existing)
        self.assertEqual(state.commit, revision)

    def test_inspect_github_candidate_ref_rejects_wrong_existing_tag(self) -> None:
        def api(path: str) -> tuple[int, dict[str, object] | None]:
            if path.endswith("/git/ref/tags/v0.1.0rc1"):
                return 200, {"object": {"type": "commit", "sha": "b" * 40}}
            return 404, {"message": "Not Found"}

        with self.assertRaisesRegex(rc.ReleaseCandidateError, "approved source revision"):
            rc.inspect_github_candidate_ref(
                "v0.1.0rc1", expected_revision="a" * 40, api=api
            )

    def test_execute_github_release_uses_argv_not_shell(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            plan = rc.github_release_plan(
                decision=decision,
                evidence=evidence,
                dist_dir=dist,
                now=DECISION_NOW,
                dry_run=False,
                inspect_ref=_absent_github_ref,
            )
            runner = MagicMock(
                return_value=subprocess.CompletedProcess(args=[], returncode=0)
            )

            def _created(tag: str, *, expected_revision: str, **_: object) -> rc.GitHubCandidateRef:
                return rc.GitHubCandidateRef(
                    tag=tag,
                    commit=expected_revision,
                    tag_present=True,
                    release_present=False,
                )

            rc.execute_github_release(
                plan,
                dist_dir=dist,
                run=runner,
                create_ref=lambda **_: 201,
                inspect_ref=_created,
            )
            runner.assert_called_once()
            args, kwargs = runner.call_args
            self.assertIsInstance(args[0], list)
            self.assertEqual(args[0][0], "gh")
            self.assertFalse(kwargs.get("shell", False))

    def test_execute_github_release_skips_mutation_when_prerelease_already_matches(
        self,
    ) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            plan = rc.github_release_plan(
                decision=decision,
                evidence=evidence,
                dist_dir=dist,
                now=DECISION_NOW,
                dry_run=False,
                inspect_ref=_reuse_github_ref(dist),
            )
            self.assertTrue(plan.reuse_existing)
            runner = MagicMock()
            create_ref = MagicMock()
            rc.execute_github_release(
                plan, dist_dir=dist, run=runner, create_ref=create_ref
            )
            runner.assert_not_called()
            create_ref.assert_not_called()

    def test_github_release_plan_rejects_conflicting_gh_repo(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with patch.dict(os.environ, {"GH_REPO": "other/repo"}):
                with self.assertRaisesRegex(rc.ReleaseCandidateError, "GH_REPO"):
                    rc.github_release_plan(
                        decision=decision,
                        evidence=evidence,
                        dist_dir=dist,
                        now=DECISION_NOW,
                        inspect_ref=_absent_github_ref,
                    )

    def test_github_release_plan_rejects_empty_or_incomplete_existing_assets(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "missing required assets"):
                rc.github_release_plan(
                    decision=decision,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_ref=_reuse_github_ref(dist, empty=True),
                )
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "missing required assets"):
                rc.github_release_plan(
                    decision=decision,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_ref=_reuse_github_ref(dist, omit="CHANGELOG.md"),
                )

    def test_github_release_plan_rejects_wrong_digest_existing_assets(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "asset digest"):
                rc.github_release_plan(
                    decision=decision,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_ref=_reuse_github_ref(dist, wrong="CHANGELOG.md"),
                )

    def test_execute_github_release_fail_closes_when_tag_appears_after_plan(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            plan = rc.github_release_plan(
                decision=decision,
                evidence=evidence,
                dist_dir=dist,
                now=DECISION_NOW,
                dry_run=False,
                inspect_ref=_absent_github_ref,
            )
            runner = MagicMock()

            def _wrong_now(tag: str, *, expected_revision: str, **_: object) -> rc.GitHubCandidateRef:
                del tag, expected_revision
                raise rc.ReleaseCandidateError(
                    "existing candidate tag does not match the approved source revision"
                )

            with self.assertRaisesRegex(rc.ReleaseCandidateError, "approved source revision"):
                rc.execute_github_release(
                    plan,
                    dist_dir=dist,
                    run=runner,
                    create_ref=lambda **_: 422,
                    inspect_ref=_wrong_now,
                )
            runner.assert_not_called()

    def test_promotion_fail_closes_on_report_count_mismatch(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            decision["platforms"]["linux/amd64"]["inherited_high_critical"] = 1
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "inherited"):
                rc.promotion_plan(
                    decision=decision,
                    image_digest=digest,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_rollback=lambda digest: None,
                )

    def _bind_decision(
        self,
        decision: dict[str, object],
        dist: Path,
        *,
        digest: str,
        revision: str,
    ) -> dict[str, object]:
        decision["source_revision"] = revision
        decision["image_digest"] = digest
        platforms = cast(dict[str, Any], decision["platforms"])
        platforms["linux/amd64"]["report_sha256"] = rc.sha256_file(dist / "delta-amd64.json")
        platforms["linux/arm64"]["report_sha256"] = rc.sha256_file(dist / "delta-arm64.json")
        decision["evidence_sha256"] = rc.sha256_file(dist / "release-evidence.json")
        return decision

    def test_promotion_fail_closes_on_failed_or_unrelated_scan_reports(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            (dist / "delta-amd64.json").write_text(
                json.dumps(
                    _sanitized_report(
                        platform="linux/arm64",
                        inherited=446,
                        expected_base_ref="nousresearch/hermes-agent@sha256:" + ("ff" * 32),
                        added=[{"id": "CVE-WRAPPER"}],
                        secrets=1,
                        failures=["wrapper adds 1 HIGH or CRITICAL", "secret findings"],
                    )
                ),
                encoding="utf-8",
            )
            artifacts = rc.collect_artifacts(dist, complete=True)
            rc.write_checksums(dist, artifacts)
            evidence = rc.build_evidence(
                source_revision=revision, image_digest=digest, artifacts=artifacts
            )
            rc.write_evidence_file(dist / "release-evidence.json", evidence)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            with self.assertRaisesRegex(
                rc.ReleaseCandidateError, "sanitized report|platform|secret|HIGH|base"
            ):
                rc.promotion_plan(
                    decision=decision,
                    image_digest=digest,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_rollback=lambda digest: None,
                )

    def test_promotion_fail_closes_when_evidence_digest_or_base_mismatches(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )
            (dist / "CHANGELOG.md").write_bytes(b"changed changelog")
            artifacts = rc.collect_artifacts(dist, complete=True)
            rc.write_checksums(dist, artifacts)
            evidence = rc.build_evidence(
                source_revision=revision, image_digest=digest, artifacts=artifacts
            )
            rc.write_evidence_file(dist / "release-evidence.json", evidence)
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "evidence"):
                rc.promotion_plan(
                    decision=decision,
                    image_digest=digest,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_rollback=lambda digest: None,
                )

    def test_promotion_fail_closes_when_rollback_image_is_not_pullable(self) -> None:
        digest = "sha256:" + ("ab" * 32)
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "rc"
            evidence = _write_complete_dist(dist, digest=digest, revision=revision)
            decision = self._bind_decision(
                self._decision(), dist, digest=digest, revision=revision
            )

            def _boom(value: str) -> None:
                raise rc.ReleaseCandidateError("previous tested rollback image is not pullable")

            with self.assertRaisesRegex(rc.ReleaseCandidateError, "not pullable"):
                rc.promotion_plan(
                    decision=decision,
                    image_digest=digest,
                    evidence=evidence,
                    dist_dir=dist,
                    now=DECISION_NOW,
                    inspect_rollback=_boom,
                )

    def test_example_owner_decision_fixture_is_secret_free_and_complete(self) -> None:
        raw = json.loads(
            _read("config/release-owner-decision.example.json")
        )
        rc.validate_owner_decision(raw, now=DECISION_NOW)
        blob = json.dumps(raw)
        self.assertNotIn("github_pat_", blob)
        self.assertFalse(raw["publication"])
        self.assertEqual(raw["visibility"], "private")
        self.assertEqual(raw["rollback_digest"], H11_DIGEST)


class PackagingScriptTests(unittest.TestCase):
    def test_package_script_builds_wheel_sdist_checksums_and_evidence(self) -> None:
        script = _read("scripts/package-release-candidate.sh")
        self.assertIn("pip wheel", script)
        self.assertIn("sdist", script)
        self.assertIn("SHA256SUMS", script)
        self.assertIn("CHANGELOG.md", script)
        self.assertIn("LICENSE", script)
        self.assertIn("NOTICE", script)
        self.assertIn("release-evidence.json", script)
        self.assertIn("hermes_helmet.release_candidate", script)
        self.assertIn("prepare-dir", script)
        self.assertIn("--root", script)
        self.assertNotIn("rm -rf \"$OUT\"", script)

    def test_prove_script_runs_setup_doctor_status_quickstart_and_company_skills(
        self,
    ) -> None:
        script = _read("scripts/prove-packaged-release.sh")
        self.assertIn("helmet setup", script)
        self.assertIn("helmet doctor", script)
        self.assertIn("helmet status", script)
        self.assertIn("install-skills", script)
        self.assertIn("setup-helmet", script)
        self.assertIn("helmet-issue", script)
        self.assertIn("helmet-epic", script)
        self.assertIn("company", script.casefold())
        self.assertIn("PYTHONPATH", script)
        self.assertIn('PYTHONPATH=""', script.replace(" ", "") + script)
        self.assertIn("prepare-dir", script)
        proof = _read("scripts/prove-packaged-release.py")
        self.assertIn('"setup"', proof)
        self.assertIn('"doctor"', proof)
        self.assertIn('"status"', proof)
        self.assertIn("status-readonly: ok", proof)
        self.assertIn("status created ledger or checkpoint state", proof)


class WorkdirSafetyTests(unittest.TestCase):
    def test_prepare_dir_refuses_preexisting_and_unsafe_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "already"
            existing.mkdir()
            marker = existing / "keep-me"
            marker.write_text("safe", encoding="utf-8")
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "already exists"):
                rc.prepare_dir(existing)
            self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "unsafe root"):
                rc.prepare_dir(Path("/tmp"))
            created = rc.prepare_dir(Path(directory) / "fresh")
            self.assertTrue(created.is_dir())
            self.assertEqual(created.parent, Path(directory).resolve())

    def test_prepare_dir_creates_project_owned_dist_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = rc.prepare_dir(root / "dist" / "rc", root=root)
            self.assertEqual(created, (root / "dist" / "rc").resolve())
            self.assertTrue((root / "dist").is_dir())
            self.assertTrue(created.is_dir())

    def test_prepare_dir_refuses_missing_parent_for_caller_supplied_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(rc.ReleaseCandidateError, "parent must already exist"):
                rc.prepare_dir(root / "elsewhere" / "out", root=root)

    def test_package_script_leaves_preexisting_dist_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "dist"
            dist.mkdir()
            marker = dist / "marker"
            marker.write_text("keep", encoding="utf-8")
            env = os.environ.copy()
            env["HERMES_HELMET_DIST_DIR"] = str(dist)
            env["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "package-release-candidate.sh")],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_prove_script_leaves_preexisting_work_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            work.mkdir()
            marker = work / "marker"
            marker.write_text("keep", encoding="utf-8")
            env = os.environ.copy()
            env["HERMES_HELMET_PROOF_WORK"] = str(work)
            env["HERMES_HELMET_DIST_DIR"] = str(Path(directory) / "missing-dist")
            env["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                ["sh", str(ROOT / "scripts" / "prove-packaged-release.sh")],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


class PackagedReleaseProofTests(unittest.TestCase):
    def test_installed_wheel_ships_cli_skills_and_passes_packaged_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "outside"
            work.mkdir()
            dist = work / "dist"
            build_venv = work / "build-venv"
            install_venv = work / "install-venv"
            run_cwd = work / "run-cwd"
            run_cwd.mkdir()

            def venv_python(path: Path) -> Path:
                return path / "bin" / "python"

            def venv_console(path: Path, name: str) -> Path:
                return path / "bin" / name

            def clean_env(**extra: str) -> dict[str, str]:
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "PYTHONPATH",
                        "PYTHONHOME",
                        "HERMES_HELMET_GH",
                        "HERMES_HELMET_HERMES",
                    }
                }
                env["PYTHONPATH"] = ""
                env.update(extra)
                return env

            venv.create(build_venv, with_pip=True, clear=True)
            build_python = venv_python(build_venv)
            bootstrap = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "pip",
                    "setuptools",
                    "wheel",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=clean_env(),
            )
            self.assertEqual(
                bootstrap.returncode,
                0,
                msg=f"{bootstrap.stdout}\n{bootstrap.stderr}",
            )
            dist.mkdir()
            # Build the wheel from the sdist so a checkout-only asset cannot hide
            # a broken published source package.
            sdist_result = subprocess.run(
                [str(build_python), "-c",
                 "from setuptools.build_meta import build_sdist; build_sdist(" + repr(str(dist)) + ")"],
                capture_output=True, text=True, check=False,
                cwd=str(ROOT), env=clean_env(),
            )
            self.assertEqual(sdist_result.returncode, 0,
                             msg=f"{sdist_result.stdout}\n{sdist_result.stderr}")
            sdists = list(dist.glob("hermes_helmet-*.tar.gz"))
            self.assertEqual(len(sdists), 1)
            built = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "-w",
                    str(dist),
                    str(sdists[0]),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(work),
                env=clean_env(),
            )
            self.assertEqual(
                built.returncode, 0, msg=f"{built.stdout}\n{built.stderr}"
            )
            wheels = sorted(dist.glob("hermes_helmet-*.whl"))
            self.assertTrue(wheels, msg=f"no wheel in {list(dist.iterdir())}")
            wheel = wheels[0]
            self.assertIn("0.1.0rc1", wheel.name)
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                # The installed skill must retain its self-contained guidance,
                # including references, without a checkout or company pack.
                for skill in rc.BUNDLED_SKILLS:
                    source = ROOT / "skills" / skill
                    for asset in source.rglob("*"):
                        if asset.is_file():
                            member = f"hermes_helmet/bundled_skills/{skill}/{asset.relative_to(source).as_posix()}"
                            self.assertEqual(archive.read(member), asset.read_bytes(), member)
            for skill in rc.BUNDLED_SKILLS:
                self.assertTrue(
                    any(
                        f"hermes_helmet/bundled_skills/{skill}/SKILL.md" in item
                        for item in names
                    ),
                    msg=f"{skill} missing from {names}",
                )
            self.assertTrue(any(item.endswith("entry_points.txt") for item in names))

            venv.create(install_venv, with_pip=True, clear=True)
            python = venv_python(install_venv)
            console = venv_console(install_venv, "helmet")
            legacy = venv_console(install_venv, "hermes-helmet")
            installed = subprocess.run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(work),
                env=clean_env(),
            )
            self.assertEqual(
                installed.returncode,
                0,
                msg=f"{installed.stdout}\n{installed.stderr}",
            )
            self.assertTrue(console.is_file())
            self.assertTrue(legacy.is_file())

            help_short = subprocess.run(
                [str(console), "--help"],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=clean_env(),
            )
            help_legacy = subprocess.run(
                [str(legacy), "--help"],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=clean_env(),
            )
            self.assertEqual(help_short.returncode, 0, msg=help_short.stderr)
            self.assertEqual(help_legacy.returncode, help_short.returncode)
            self.assertEqual(help_legacy.stdout, help_short.stdout)
            self.assertEqual(help_legacy.stderr, help_short.stderr)

            missing_short = subprocess.run(
                [str(console)],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=clean_env(),
            )
            missing_legacy = subprocess.run(
                [str(legacy)],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=clean_env(),
            )
            self.assertNotEqual(missing_short.returncode, 0)
            self.assertEqual(missing_legacy.returncode, missing_short.returncode)
            self.assertEqual(missing_legacy.stdout, missing_short.stdout)
            self.assertEqual(missing_legacy.stderr, missing_short.stderr)

            probe = subprocess.run(
                [
                    str(python),
                    str(ROOT / "scripts" / "prove-packaged-release.py"),
                    "--console",
                    str(console),
                    "--fixture-root",
                    str(ROOT),
                    "--work",
                    str(work / "proof"),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(run_cwd),
                env=clean_env(),
            )
            self.assertEqual(
                probe.returncode,
                0,
                msg=f"{probe.stdout}\n{probe.stderr}",
            )
            self.assertIn("setup: ok", probe.stdout)
            self.assertIn("doctor: ok", probe.stdout)
            self.assertIn("status: ok", probe.stdout)
            self.assertIn("status-readonly: ok", probe.stdout)
            self.assertIn("quickstart: ok", probe.stdout)
            self.assertIn("company-skills: ok", probe.stdout)
            self.assertIn("bundled-skills: ok", probe.stdout)


if __name__ == "__main__":
    unittest.main()
