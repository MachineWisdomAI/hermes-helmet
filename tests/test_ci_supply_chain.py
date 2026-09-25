#!/usr/bin/env python3
"""Public CI and pinned-toolchain tests."""

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
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    for path in paths:
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

    def test_github_cli_uses_checksummed_architecture_pin(self) -> None:
        dockerfile = _read("deploy/Dockerfile")
        wrapper = _read("deploy/hermes/gh-runtime-wrapper.sh")
        self.assertIn("ARG GH_CLI_VERSION=2.101.0", dockerfile)
        self.assertIn("sha256sum -c -", dockerfile)
        self.assertIn('case "${TARGETARCH}" in', dockerfile)
        self.assertIn("install -m 0755", dockerfile)
        self.assertIn("/usr/local/libexec/hermes-helmet/gh --version", dockerfile)
        self.assertIn("upstream_gh=/usr/local/libexec/hermes-helmet/gh", wrapper)

    def test_every_third_party_action_is_pinned_to_a_commit_sha(self) -> None:
        texts = _workflow_texts()
        self.assertTrue(texts, "expected GitHub workflow files")
        unpinned: list[str] = []
        for name, workflow in texts.items():
            for match in USES_RE.finditer(workflow):
                spec = match.group(1)
                if spec.startswith("./"):
                    continue
                if "@" not in spec or not PINNED_SHA_RE.match(spec.rsplit("@", 1)[1]):
                    unpinned.append(f"{name}:{spec}")
        self.assertEqual(unpinned, [])

    def test_jj_install_uses_checksummed_pin_not_legacy_installer(self) -> None:
        combined = "\n".join(_workflow_texts().values())
        self.assertNotIn(LEGACY_JJ, combined)
        self.assertNotIn("install-jj.sh | sh", combined)
        self.assertNotIn("raw.githubusercontent.com/jj-vcs/jj", combined)
        self.assertIn("hermes_helmet.jj_toolchain", combined)

    def test_source_security_scan_and_sbom_are_configured(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        self.assertIn("aquasecurity/trivy-action@", workflow)
        self.assertIn("scan-type: fs", workflow)
        self.assertIn("scanners: vuln,secret", workflow)
        self.assertIn("format: spdx-json", workflow)
        self.assertIn("source-sbom", workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', workflow)

    def test_public_ci_contains_no_private_release_operations(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        for marker in (
            "release-evidence",
            "dogfood",
            "ghcr.io/machinewisdomai",
            "push: true",
            "ENABLE_PRIVATE_IMAGE_PUBLISH",
            "package-release-candidate.sh",
            "promote-release-candidate.sh",
            "prove-packaged-release",
        ):
            self.assertNotIn(marker, workflow)

    def test_release_candidate_job_verifies_distributable_packaging(self) -> None:
        workflow = _read(".github/workflows/verify.yml")
        jobs = re.split(r"(?m)^(?=  [A-Za-z0-9_-]+:)", workflow)
        job = next(item for item in jobs if item.startswith("  release-candidate:"))
        self.assertIn("Confirm packaging checkout matches recorded source", job)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', job)
        self.assertIn("python -m unittest tests.test_packaged_release -v", job)
        self.assertNotIn("scripts/package-release-candidate.sh", job)
        self.assertNotIn("ghcr.io/", job)

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
