#!/usr/bin/env python3
"""Targeted checks for the public preview runtime-image workflow."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "preview-runtime-image.yml"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s+uses:\s+(\S+)", re.MULTILINE)
BASE_IMAGE = (
    "nousresearch/hermes-agent:v2026.9.14@"
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
BASE_AMD64 = (
    "nousresearch/hermes-agent@"
    "sha256:1f983df4d778d46b3c3892d7598c9d57291430a6e35936ebc371ae1e5766699a"
)
BASE_ARM64 = (
    "nousresearch/hermes-agent@"
    "sha256:011f2e232f119a9601cf6525f7542921d758703c7771ce2f7b639a07fa8640d1"
)
RUNTIME = "ghcr.io/machinewisdomai/hermes-helmet/runtime"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _jobs(workflow: str) -> dict[str, str]:
    start = workflow.index("\njobs:\n")
    body = workflow[start + len("\njobs:\n") :]
    jobs: dict[str, str] = {}
    for chunk in re.split(r"(?m)^(?=  [A-Za-z0-9_-]+:)", body):
        if not chunk.strip():
            continue
        name = chunk.split(":", 1)[0].strip()
        jobs[name] = chunk
    return jobs


def _package_version() -> str:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    return str(pyproject["project"]["version"])


class PreviewRuntimeImageWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.jobs = _jobs(self.workflow)

    def test_workflow_exists_as_a_dedicated_public_file(self) -> None:
        self.assertTrue(WORKFLOW.is_file())
        self.assertIn("Preview runtime image", self.workflow)
        self.assertIn("workflow_dispatch", self.workflow)
        self.assertNotIn("hermes-helmet-oss", self.workflow)
        self.assertNotIn("ENABLE_PRIVATE_IMAGE_PUBLISH", self.workflow)
        self.assertNotIn("release-evidence", self.workflow)
        self.assertNotIn("dogfood", self.workflow)
        self.assertNotIn("release_candidate", self.workflow)

    def test_dispatch_requires_full_sha_on_protected_main(self) -> None:
        resolve = self.jobs["resolve-source"]
        publish = self.jobs["publish"]
        self.assertIn("github.event.inputs.source_sha", resolve)
        self.assertIn('GITHUB_REF" != "refs/heads/main"', resolve)
        self.assertIn("MachineWisdomAI/hermes-helmet", resolve)
        self.assertIn(r"^[0-9a-f]{40}$", resolve)
        self.assertIn("compare/main...${SOURCE_SHA}", resolve)
        self.assertIn(".ahead_by", resolve)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", publish)
        self.assertIn('GITHUB_EVENT_NAME" != "workflow_dispatch"', publish)
        self.assertIn('GITHUB_REF" != "refs/heads/main"', publish)
        self.assertIn("compare/main...${SOURCE_COMMIT}", publish)

    def test_source_checkout_uses_the_recorded_full_sha(self) -> None:
        resolve = self.jobs["resolve-source"]
        build = self.jobs["build"]
        self.assertIn("github.event.inputs.source_sha", resolve)
        self.assertIn("github.event.pull_request.head.sha", resolve)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"', build)
        self.assertIn("ref: ${{ env.SOURCE_COMMIT }}", build)
        self.assertIn("persist-credentials: false", resolve)
        self.assertIn("persist-credentials: false", build)

    def test_expected_version_comes_from_the_package_declaration(self) -> None:
        version = _package_version()
        init = _read("src/hermes_helmet/__init__.py")
        smoke = _read("scripts/smoke-dev-image.sh")
        self.assertIn(f'__version__ = "{version}"', init)
        self.assertIn('expected_version="${HERMES_HELMET_VERSION:-0.0.0-dev}"', smoke)
        self.assertIn("package_version=", self.jobs["resolve-source"])
        self.assertIn("HERMES_HELMET_VERSION=${{ env.PACKAGE_VERSION }}", self.jobs["build"])
        self.assertIn("HERMES_HELMET_VERSION: ${{ env.PACKAGE_VERSION }}", self.jobs["build"])
        self.assertIn(
            'test "$version" = "$PACKAGE_VERSION"',
            self.jobs["build"],
        )

    def test_pinned_base_and_platforms_match_the_dockerfile(self) -> None:
        dockerfile = _read("deploy/Dockerfile")
        self.assertIn(f"ARG HERMES_BASE_IMAGE={BASE_IMAGE}", dockerfile)
        self.assertIn(f"HERMES_BASE_IMAGE: {BASE_IMAGE}", self.workflow)
        self.assertIn(f"HERMES_BASE_AMD64_IMAGE: {BASE_AMD64}", self.workflow)
        self.assertIn(f"HERMES_BASE_ARM64_IMAGE: {BASE_ARM64}", self.workflow)
        self.assertIn("linux/amd64", self.workflow)
        self.assertIn("linux/arm64", self.workflow)
        self.assertIn("ubuntu-latest", self.jobs["build"])
        self.assertIn("ubuntu-24.04-arm", self.jobs["build"])
        self.assertNotIn("docker/setup-qemu-action@", self.workflow)
        self.assertIn("deploy/Dockerfile", self.jobs["build"])

    def test_permission_separation_keeps_publication_off_pull_requests(self) -> None:
        header, _jobs_body = self.workflow.split("\njobs:\n", 1)
        self.assertIn("permissions:\n  contents: read\n", header)
        self.assertNotIn("packages: write", header)
        self.assertNotIn("packages: write", self.jobs["resolve-source"])
        self.assertNotIn("packages: write", self.jobs["build"])
        self.assertNotIn("packages: write", self.jobs["summarize"])
        self.assertIn("packages: write", self.jobs["publish"])
        self.assertIn("contents: read", self.jobs["publish"])
        self.assertNotIn("docker/login-action@", self.jobs["build"])
        self.assertNotIn("docker/login-action@", self.jobs["resolve-source"])
        self.assertIn("docker/login-action@", self.jobs["publish"])
        self.assertNotIn("pull_request_target", self.workflow)

    def test_artifact_identity_is_recorded_before_any_push(self) -> None:
        build = self.jobs["build"]
        publish = self.jobs["publish"]
        self.assertIn("outputs: type=docker,dest=dist/preview/${{ matrix.arch }}/hermes-helmet-linux-${{ matrix.arch }}.tar", build)
        self.assertIn("identity.json", build)
        self.assertIn("SHA256SUMS", build)
        self.assertIn("sbom.spdx.json", build)
        self.assertIn("delta.json", build)
        self.assertIn("preview-runtime-linux-${{ matrix.arch }}", build)
        self.assertIn("sha256sum --check SHA256SUMS", publish)
        self.assertIn("Download tested image artifacts from this run", publish)
        self.assertIn("pattern: preview-runtime-linux-*", publish)

    def test_publish_does_not_rebuild_and_verifies_manifest_platforms(self) -> None:
        publish = self.jobs["publish"]
        self.assertNotIn("docker/build-push-action@", publish)
        self.assertNotIn("docker build ", publish)
        self.assertIn("docker load -i dist/preview/preview-runtime-linux-amd64/hermes-helmet-linux-amd64.tar", publish)
        self.assertIn("docker load -i dist/preview/preview-runtime-linux-arm64/hermes-helmet-linux-arm64.tar", publish)
        self.assertIn("docker buildx imagetools create", publish)
        self.assertIn("docker buildx imagetools inspect", publish)
        self.assertIn('assert set(platforms) >= {"linux/amd64", "linux/arm64"}', publish)
        self.assertIn("${IMAGE_REPOSITORY}:${PREVIEW_TAG}", publish)
        self.assertNotIn(":latest", self.workflow)
        self.assertNotIn(":stable", self.workflow)
        self.assertNotIn("tags: latest", self.workflow)
        self.assertIn("preview-${source_commit}", self.jobs["resolve-source"])
        self.assertNotIn("/visibility", self.workflow)
        self.assertNotIn("packages/container/", self.workflow)

    def test_destination_does_not_touch_existing_packages(self) -> None:
        self.assertIn(RUNTIME, self.workflow)
        self.assertNotIn("ghcr.io/machinewisdomai/hermes-helmet:", self.workflow)
        self.assertNotIn("ghcr.io/machinewisdomai/hermes-helmet-oss", self.workflow)
        verify = VERIFY.read_text(encoding="utf-8")
        self.assertNotIn("ghcr.io/machinewisdomai", verify)
        self.assertNotIn("push: true", verify)
        self.assertNotIn("packages: write", verify)

    def test_scan_and_smoke_run_on_the_built_bytes(self) -> None:
        build = self.jobs["build"]
        self.assertIn("scripts/compare_trivy_reports.py", build)
        self.assertIn("scripts/smoke-dev-image.sh", build)
        self.assertIn('--expected-base-ref "$HERMES_BASE_IMAGE"', build)
        self.assertIn("image-ref: hermes-helmet:preview-linux-${{ matrix.arch }}", build)
        self.assertIn("image-ref: hermes-base:scan-${{ matrix.arch }}", build)
        self.assertIn('TRIVY_SKIP_DB_UPDATE: "true"', build)
        self.assertIn("cache: false", build)
        self.assertIn('rm -f "$OUT_DIR/base.json" "$OUT_DIR/derived.json"', build)
        self.assertNotRegex(build, r"(?m)^\s+severity:")
        self.assertIn("inherited_high_or_critical", self.jobs["summarize"])
        self.assertIn("inherited_high_or_critical", self.jobs["publish"])
        self.assertNotIn('delta.get("base", {}).get("high_or_critical")', self.jobs["summarize"])
        build_index = build.index("Build ${{ matrix.platform }} image once")
        scan_index = build.index("Scan ${{ matrix.platform }} image")
        smoke_index = build.index("Smoke-test tested ${{ matrix.platform }} bytes")
        upload_index = build.index("Upload tested ${{ matrix.platform }} artifacts")
        self.assertLess(build_index, scan_index)
        self.assertLess(scan_index, smoke_index)
        self.assertLess(smoke_index, upload_index)

    def test_actions_are_pinned_consistently_with_verify(self) -> None:
        verify = VERIFY.read_text(encoding="utf-8")
        for action in (
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "docker/setup-buildx-action@e468171a9de216ec08956ac3ada2f0791b6bd435",
            "docker/build-push-action@263435318d21b8e681c14492fe198d362a7d2c83",
            "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
            "docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772",
        ):
            self.assertIn(action, self.workflow)
        for match in USES_RE.finditer(self.workflow):
            spec = match.group(1)
            _name, digest = spec.rsplit("@", 1)
            self.assertRegex(digest, PINNED_SHA_RE.pattern)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", verify)
        self.assertIn("aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25", verify)

    def test_run_instructions_cover_owner_visibility_and_inherited_findings(self) -> None:
        combined = self.jobs["publish"] + self.jobs["summarize"]
        self.assertIn("Owner package-visibility step", combined)
        self.assertIn("Inherited findings", combined)
        self.assertIn("Anonymous digest pull", combined)
        self.assertIn("does not change visibility", combined)
        self.assertIn("GITHUB_STEP_SUMMARY", combined)

    def test_oci_labels_record_source_revision_and_version(self) -> None:
        build = self.jobs["build"]
        self.assertIn("org.opencontainers.image.source=https://github.com/MachineWisdomAI/hermes-helmet", build)
        self.assertIn("org.opencontainers.image.revision=${{ env.SOURCE_COMMIT }}", build)
        self.assertIn("org.opencontainers.image.version=${{ env.PACKAGE_VERSION }}", build)
        self.assertIn("index:org.opencontainers.image.revision=${SOURCE_COMMIT}", self.jobs["publish"])


class RestoredComparatorPresenceTests(unittest.TestCase):
    def test_generic_comparator_and_negative_tests_are_present(self) -> None:
        script = ROOT / "scripts" / "compare_trivy_reports.py"
        tests = ROOT / "tests" / "test_compare_trivy_reports.py"
        self.assertTrue(script.is_file())
        self.assertTrue(tests.is_file())
        text = tests.read_text(encoding="utf-8")
        self.assertIn("test_wrapper_added_high_or_critical_finding_fails", text)
        self.assertIn("test_any_derived_secret_fails", text)
        self.assertIn("test_lower_severity_derived_secret_fails", text)
        self.assertIn("test_retained_inherited_report_excludes_removed_findings", text)
        self.assertIn("test_platform_layer_base_label_and_scanner_mismatches_fail", text)
        self.assertIn("test_incomplete_evidence_fails_closed", text)
        self.assertIn("test_severity_worsening_fails_as_an_added_identity", text)


if __name__ == "__main__":
    unittest.main()
