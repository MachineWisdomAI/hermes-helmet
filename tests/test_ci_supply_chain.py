#!/usr/bin/env python3
"""H11 CI product proof and supply-chain pin tests."""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_helmet.jj_toolchain import (
    ARTIFACTS,
    JJ_VERSION,
    JjToolchainError,
    artifact_triple,
    install,
    require_version,
    require_version_output,
    validate_binary,
    verify_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
USES_RE = re.compile(r"^\s+uses:\s+(\S+)", re.MULTILINE)
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LEGACY_JJ = "0.28.0"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _workflow_texts() -> dict[str, str]:
    texts = {}
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        texts[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return texts


class JjPinTests(unittest.TestCase):
    @staticmethod
    def _archive(binary: bytes = b"synthetic-jj-binary") -> bytes:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            info = tarfile.TarInfo("jj")
            info.size = len(binary)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(binary))
        return payload.getvalue()

    def test_accepted_pin_is_jujutsu_0_45_1(self) -> None:
        self.assertEqual(JJ_VERSION, "0.45.1")
        require_version("0.45.1")
        self.assertIn("x86_64-unknown-linux-musl", ARTIFACTS)
        self.assertIn("aarch64-unknown-linux-musl", ARTIFACTS)
        self.assertIn("x86_64-apple-darwin", ARTIFACTS)
        self.assertIn("aarch64-apple-darwin", ARTIFACTS)
        for triple, (filename, digest) in ARTIFACTS.items():
            self.assertTrue(filename.endswith(".tar.gz"), triple)
            self.assertIn(JJ_VERSION, filename)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            ARTIFACTS["x86_64-unknown-linux-musl"][1],
            "f35438350b5d61963aac5dd74ede510b31d6b9690769d1a6268cf058cc825f72",
        )
        self.assertEqual(
            ARTIFACTS["aarch64-unknown-linux-musl"][1],
            "7349a43dd5a20dbc998b10114daa0ee63d2ab863fb822c7eb6b0ebca5903cc69",
        )

    def test_version_drift_is_rejected(self) -> None:
        require_version_output("jj 0.45.1-7c41cdeb16b6b321c64e789a966b6adf723816a5\n")
        with self.assertRaisesRegex(JjToolchainError, "version drift"):
            require_version(LEGACY_JJ)
        with self.assertRaisesRegex(JjToolchainError, "version drift"):
            require_version("latest")
        with self.assertRaisesRegex(JjToolchainError, "version drift"):
            require_version_output("jj 0.45.10\n")
        with self.assertRaisesRegex(JjToolchainError, "unrecognized"):
            require_version_output("prefix jj 0.45.1 suffix\n")

    def test_invoked_binary_version_drift_is_rejected(self) -> None:
        exact = mock.Mock(returncode=0, stdout="jj 0.45.1-release\n")
        drifted = mock.Mock(returncode=0, stdout="jj 0.45.10\n")
        with mock.patch("hermes_helmet.jj_toolchain.subprocess.run", return_value=exact):
            validate_binary(Path("/synthetic/jj"))
        with mock.patch(
            "hermes_helmet.jj_toolchain.subprocess.run", return_value=drifted
        ), self.assertRaisesRegex(JjToolchainError, "version drift"):
            validate_binary(Path("/synthetic/jj"))

    def test_checksum_drift_is_rejected(self) -> None:
        _filename, expected = ARTIFACTS["x86_64-unknown-linux-musl"]
        payload = b"not-a-jj-archive"
        self.assertNotEqual(hashlib.sha256(payload).hexdigest(), expected)
        with self.assertRaisesRegex(JjToolchainError, "checksum"):
            verify_sha256(payload, expected)
        verify_sha256(payload, hashlib.sha256(payload).hexdigest())

    def test_install_verifies_before_atomic_binary_replacement(self) -> None:
        archive = self._archive()
        digest = hashlib.sha256(archive).hexdigest()
        triple = "x86_64-unknown-linux-musl"
        artifact = (f"jj-v{JJ_VERSION}-{triple}.tar.gz", digest)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            ARTIFACTS, {triple: artifact}, clear=True
        ), mock.patch(
            "hermes_helmet.jj_toolchain.artifact_triple", return_value=triple
        ), mock.patch(
            "hermes_helmet.jj_toolchain.request.urlopen",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(archive),
        ):
            destination = install(bin_dir=Path(directory))
            self.assertEqual(destination.read_bytes(), b"synthetic-jj-binary")
            self.assertTrue(destination.stat().st_mode & 0o111)

            ARTIFACTS[triple] = (artifact[0], "0" * 64)
            destination.unlink()
            with self.assertRaisesRegex(JjToolchainError, "checksum"):
                install(bin_dir=Path(directory))
            self.assertFalse(destination.exists())

    def test_platform_triples_cover_ci_runners(self) -> None:
        self.assertEqual(
            artifact_triple(system="Linux", machine="x86_64"),
            "x86_64-unknown-linux-musl",
        )
        self.assertEqual(
            artifact_triple(system="Linux", machine="aarch64"),
            "aarch64-unknown-linux-musl",
        )
        self.assertEqual(
            artifact_triple(system="Darwin", machine="arm64"),
            "aarch64-apple-darwin",
        )
        self.assertEqual(
            artifact_triple(system="Darwin", machine="x86_64"),
            "x86_64-apple-darwin",
        )
        with self.assertRaisesRegex(JjToolchainError, "unsupported"):
            artifact_triple(system="Windows", machine="AMD64")


class WorkflowSupplyChainTests(unittest.TestCase):
    def test_smoke_check_matches_the_pinned_hermes_release(self) -> None:
        smoke = _read("scripts/smoke-dev-image.sh")
        self.assertIn("expected Hermes Agent 0.21.3", smoke)
        self.assertIn("*0.21.3*", smoke)
        self.assertNotIn("expected Hermes Agent 0.21.1", smoke)

    def test_github_cli_uses_current_checksummed_architecture_pin(self) -> None:
        dockerfile = _read("deploy/Dockerfile")
        wrapper = _read("deploy/hermes/gh-runtime-wrapper.sh")
        self.assertIn("ARG GH_CLI_VERSION=2.101.0", dockerfile)
        self.assertIn(
            "ARG GH_CLI_SHA256_AMD64="
            "9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8",
            dockerfile,
        )
        self.assertIn(
            "ARG GH_CLI_SHA256_ARM64="
            "b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f",
            dockerfile,
        )
        self.assertIn('case "${TARGETARCH}" in', dockerfile)
        self.assertIn('*) echo "unsupported GitHub CLI architecture:', dockerfile)
        checksum = dockerfile.index("sha256sum -c -")
        install = dockerfile.index("install -m 0755")
        self.assertLess(checksum, install)
        self.assertNotIn("apt-get install", dockerfile)
        self.assertNotIn("GH_PACKAGE_VERSION", dockerfile)
        self.assertIn(
            "/usr/local/libexec/hermes-helmet/gh --version", dockerfile
        )
        self.assertIn(
            "upstream_gh=/usr/local/libexec/hermes-helmet/gh", wrapper
        )
        self.assertNotIn("exec gh", wrapper)

    def test_every_third_party_action_is_pinned_to_a_commit_sha(self) -> None:
        texts = _workflow_texts()
        self.assertTrue(texts, "expected GitHub workflow files")
        unpinned: list[str] = []
        for name, text in texts.items():
            for match in USES_RE.finditer(text):
                spec = match.group(1)
                if spec.startswith("./"):
                    continue
                if "@" not in spec:
                    unpinned.append(f"{name}:{spec}")
                    continue
                _action, ref = spec.rsplit("@", 1)
                if not PINNED_SHA_RE.match(ref):
                    unpinned.append(f"{name}:{spec}")
        self.assertEqual(unpinned, [])

    def test_jj_install_uses_checksummed_pin_not_legacy_installer(self) -> None:
        combined = "\n".join(_workflow_texts().values())
        self.assertNotIn(LEGACY_JJ, combined)
        self.assertNotIn("install-jj.sh | sh", combined)
        self.assertNotIn("raw.githubusercontent.com/jj-vcs/jj", combined)
        self.assertIn("hermes_helmet.jj_toolchain", combined)

    def test_images_build_linux_amd64_and_arm64_with_sbom_and_provenance(self) -> None:
        combined = "\n".join(_workflow_texts().values())
        self.assertIn("linux/amd64", combined)
        self.assertIn("linux/arm64", combined)
        self.assertRegex(combined, r"provenance:\s*(?:true|mode=max)")
        self.assertRegex(combined, r"sbom:\s*true")
        self.assertIn("docker/setup-qemu-action@", combined)

    def test_secret_and_vulnerability_scanning_and_source_sbom_are_configured(self) -> None:
        combined = "\n".join(_workflow_texts().values())
        self.assertIn("aquasecurity/trivy-action@", combined)
        self.assertIn("scan-type: fs", combined)
        self.assertIn("scan-type: image", combined)
        self.assertIn("vuln", combined)
        self.assertIn("secret", combined)
        self.assertIn("format: spdx-json", combined)
        self.assertIn("actions/upload-artifact@", combined)
        self.assertIn("image-ref: hermes-helmet:scan-amd64", combined)
        self.assertIn("image-ref: hermes-helmet:scan-arm64", combined)
        self.assertIn("image-ref: hermes-base:scan-amd64", combined)
        self.assertIn("image-ref: hermes-base:scan-arm64", combined)
        self.assertIn(
            "HERMES_BASE_AMD64_IMAGE: nousresearch/hermes-agent@"
            "sha256:1f983df4d778d46b3c3892d7598c9d57291430a6e35936ebc371ae1e5766699a",
            combined,
        )
        self.assertIn(
            "HERMES_BASE_ARM64_IMAGE: nousresearch/hermes-agent@"
            "sha256:011f2e232f119a9601cf6525f7542921d758703c7771ce2f7b639a07fa8640d1",
            combined,
        )
        self.assertIn("scripts/compare_trivy_reports.py", combined)
        self.assertIn("image-security-evidence", combined)

    def test_both_platform_images_are_scanned_before_publication(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        amd64_scan = workflow.index("Scan linux/amd64 image before publication")
        arm64_scan = workflow.index("Scan linux/arm64 image before publication")
        amd64_gate = workflow.index("Enforce clean linux/amd64 wrapper delta")
        arm64_gate = workflow.index("Enforce clean linux/arm64 wrapper delta")
        amd64_release = workflow.index("Release linux/amd64 scan layers")
        arm64_build = workflow.index("Build linux/arm64 image for pre-publish scan")
        arm64_release = workflow.index("Release linux/arm64 scan layers")
        evidence_upload = workflow.index("Upload sanitized image scan evidence")
        private_gate = workflow.index(
            "Require private GHCR package before development-image publication"
        )
        registry_login = workflow.index("Log in to GHCR after security gates pass")
        publication = workflow.index("push: true")
        self.assertLess(amd64_scan, publication)
        self.assertLess(arm64_scan, publication)
        self.assertLess(amd64_gate, registry_login)
        self.assertLess(arm64_gate, registry_login)
        self.assertLess(amd64_release, arm64_build)
        self.assertLess(arm64_release, publication)
        self.assertLess(evidence_upload, registry_login)
        self.assertLess(private_gate, registry_login)
        self.assertLess(registry_login, publication)
        self.assertIn("image-ref: hermes-helmet:scan-amd64", workflow)
        self.assertIn("image-ref: hermes-helmet:scan-arm64", workflow)
        self.assertIn("--platform linux/amd64", workflow)
        self.assertIn("--platform linux/arm64", workflow)
        self.assertEqual(workflow.count("--expected-base-ref \"$HERMES_BASE_IMAGE\""), 2)
        self.assertEqual(workflow.count("--database-sha256-file"), 2)
        self.assertEqual(workflow.count("--java-database-sha256-file"), 2)
        self.assertEqual(workflow.count('TRIVY_SKIP_DB_UPDATE: "true"'), 3)
        self.assertEqual(workflow.count('TRIVY_SKIP_JAVA_DB_UPDATE: "true"'), 3)
        self.assertEqual(workflow.count("          cache: false"), 3)
        self.assertNotIn("skip-db-update:", workflow)
        self.assertNotIn("skip-java-db-update:", workflow)

    def test_development_image_checkout_matches_its_claimed_source_commit(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        dev_job = workflow[workflow.index("  dev-image:") :]
        checkout = dev_job.index("uses: actions/checkout@")
        source_ref = dev_job.index("ref: ${{ env.SOURCE_COMMIT }}")
        first_build = dev_job.index("Build linux/amd64 image for pre-publish scan")
        self.assertLess(checkout, source_ref)
        self.assertLess(source_ref, first_build)

    def test_release_candidate_checkout_matches_its_claimed_source_commit(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        job = workflow[workflow.index("  release-candidate:") : workflow.index("  dev-image:")]
        self.assertIn("ref: ${{ env.SOURCE_COMMIT }}", job)
        self.assertLess(
            job.index("ref: ${{ env.SOURCE_COMMIT }}"),
            job.index("scripts/package-release-candidate.sh"),
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', job)

    def test_security_job_scans_recorded_source_commit(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        job = workflow[workflow.index("  security:") : workflow.index("  release-candidate:")]
        self.assertIn("SOURCE_COMMIT: ${{ github.event.pull_request.head.sha || github.sha }}", job)
        self.assertIn("ref: ${{ env.SOURCE_COMMIT }}", job)
        self.assertLess(
            job.index("ref: ${{ env.SOURCE_COMMIT }}"),
            job.index("Generate source SBOM"),
        )
        self.assertLess(
            job.index("ref: ${{ env.SOURCE_COMMIT }}"),
            job.index("Scan source, dependencies, and secrets"),
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', job)

    def test_all_image_scans_share_one_recorded_database_snapshot(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        self.assertEqual(
            workflow.count("cache-dir: ${{ github.workspace }}/.cache/trivy"), 4
        )
        self.assertIn(
            "sha256sum .cache/trivy/db/trivy.db > dist/trivy/trivy-db.sha256",
            workflow,
        )
        self.assertGreaterEqual(
            workflow.count("sha256sum --check dist/trivy/trivy-db.sha256"), 3
        )
        self.assertIn("dist/trivy/trivy-db-metadata.json", workflow)
        self.assertIn("dist/trivy/trivy-java-db.sha256", workflow)
        self.assertIn("dist/trivy/trivy-java-db-metadata.json", workflow)
        self.assertGreaterEqual(
            workflow.count("grep -qx absent dist/trivy/trivy-java-db.sha256"), 3
        )
        recorded = workflow.index("Record Trivy database snapshot")
        for step in (
            "Scan linux/amd64 image before publication",
            "Scan linux/arm64 base image",
            "Scan linux/arm64 image before publication",
        ):
            start = workflow.index(step, recorded)
            end = workflow.find("      - name:", start + len(step))
            section = workflow[start : end if end >= 0 else None]
            self.assertIn("cache: false", section)

    def test_raw_secret_scan_reports_are_not_uploaded(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        upload = workflow[workflow.index("Upload sanitized image scan evidence") :]
        self.assertIn("dist/trivy/delta-*.json", upload)
        self.assertNotIn("dist/trivy/base-*.json", upload)
        self.assertNotIn("dist/trivy/derived-*.json", upload)
        self.assertIn(
            "rm -f dist/trivy/base-amd64.json dist/trivy/derived-amd64.json",
            workflow,
        )
        self.assertIn(
            "rm -f dist/trivy/base-arm64.json dist/trivy/derived-arm64.json",
            workflow,
        )

    def test_private_dev_image_is_not_documented_as_release_acceptance(self) -> None:
        policy = _read("SECURITY.md")
        normalized = " ".join(policy.split())
        self.assertIn(
            "The private `dev-<source-commit>` image is a CI verification artifact",
            normalized,
        )
        self.assertIn("not a numbered release", normalized)
        self.assertIn("requires an explicit owner decision", normalized)

    def test_development_image_publication_requires_private_package(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        self.assertIn(
            '"/orgs/${GITHUB_REPOSITORY_OWNER}/packages/container/hermes-helmet-oss"',
            workflow,
        )
        self.assertIn('test "$visibility" = private', workflow)

    def test_ci_runs_on_linux_and_macos(self) -> None:
        combined = "\n".join(_workflow_texts().values())
        self.assertIn("ubuntu-latest", combined)
        self.assertIn("macos-latest", combined)

    def test_dependabot_covers_actions_and_docker(self) -> None:
        text = _read(".github/dependabot.yml")
        self.assertIn("package-ecosystem: github-actions", text)
        self.assertIn("package-ecosystem: docker", text)
        self.assertIn("directory: /deploy", text)


if __name__ == "__main__":
    unittest.main()
