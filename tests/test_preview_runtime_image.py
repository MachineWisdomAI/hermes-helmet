#!/usr/bin/env python3
"""Targeted checks for the public preview runtime-image workflow."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "preview-runtime-image.yml"
VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s+uses:\s+(\S+)", re.MULTILINE)
BASE_IMAGE = (
    "nousresearch/hermes-agent:v0.21.6@"
    "sha256:55e192fba0cd4fde61142abbff5adeacff40efdb482ccd0ff877924bd274f909"
)
BASE_AMD64 = (
    "nousresearch/hermes-agent@"
    "sha256:32d095e84190ae7266afbb361579e6921f2c633e899f5eacfb88111b9c851a22"
)
BASE_ARM64 = (
    "nousresearch/hermes-agent@"
    "sha256:93b295a42bdb6003e06fb9ce316bd6d4f4c09a93a7a89d263701ec6b1a79b208"
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

    def test_failed_gate_retains_only_sanitized_diagnostics(self) -> None:
        build = self.jobs["build"]
        enforce_index = build.index("Enforce clean ${{ matrix.platform }} wrapper delta")
        retain_index = build.index("Retain failed ${{ matrix.platform }} wrapper-delta diagnostics")
        sbom_index = build.index("Generate ${{ matrix.platform }} SBOM from tested bytes")
        self.assertLess(enforce_index, retain_index)
        self.assertLess(retain_index, sbom_index)
        step = build[retain_index:sbom_index]
        self.assertIn("id: wrapper_delta", build[enforce_index:retain_index])
        self.assertIn("failure() && steps.wrapper_delta.outcome == 'failure'", step)
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", step)
        for retained in ("delta.json", "trivy-db.sha256", "trivy-db-metadata.json"):
            self.assertIn(f"dist/preview/${{{{ matrix.arch }}}}/{retained}", step)
        # Raw Trivy JSON and the image tarball are never retained on failure,
        # and the diagnostic artifact cannot be mistaken for a tested artifact.
        for forbidden in ("base.json", "derived.json", ".tar", "sbom"):
            self.assertNotIn(forbidden, step)
        self.assertNotIn("name: preview-runtime-linux-", step)

    def test_actions_are_pinned_consistently_with_verify(self) -> None:
        verify = VERIFY.read_text(encoding="utf-8")
        for action in (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069",
            "docker/build-push-action@263435318d21b8e681c14492fe198d362a7d2c83",
            "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
            "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
            "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
            "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
        ):
            self.assertIn(action, self.workflow)
        for match in USES_RE.finditer(self.workflow):
            spec = match.group(1)
            _name, digest = spec.rsplit("@", 1)
            self.assertRegex(digest, PINNED_SHA_RE.pattern)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", verify)
        self.assertIn("aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25", verify)

    def test_download_artifact_keeps_strict_digest_and_archive_handling(self) -> None:
        # download-artifact v8 errors on digest mismatch by default and skips
        # unzip only for non-zipped uploads. Keep both defaults: no override
        # may weaken the digest check or leave archives undecompressed.
        for override in ("digest-mismatch", "skip-decompress", "merge-multiple"):
            self.assertNotIn(override, self.workflow)

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


GSTACK_COMMIT = "47d639abffa882b8cbb641d7d1dedf08ecada9ec"
GSTACK_BUNDLE_SHA256 = "ec759ca41239ff888acdd0654c3f6ef505e3314b87c6e5de11903f804fc8a859"
WRITING = ROOT / "deploy" / "writing"
IMAGE_INTERPRETERS = (
    "/opt/hermes/.venv/bin/python",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
)


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _writing_stage(dockerfile: str) -> str:
    start = dockerfile.index("FROM helmet AS writing")
    return dockerfile[start : dockerfile.index("FROM helmet AS runtime")]


class WritingImageDockerfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dockerfile = _read("deploy/Dockerfile")
        self.stage = _writing_stage(self.dockerfile)

    def test_writing_is_a_target_and_a_plain_build_stays_the_unchanged_image(self) -> None:
        stages = re.findall(r"(?m)^FROM \S+ AS (\S+)$", self.dockerfile)
        self.assertEqual(stages, ["helmet", "writing", "runtime"])
        self.assertTrue(self.dockerfile.rstrip().endswith("FROM helmet AS runtime"))
        # The Helmet stage keeps the existing entrypoint; the writing stage reuses it.
        self.assertEqual(self.dockerfile.count("ENTRYPOINT"), 1)
        self.assertNotIn("ENTRYPOINT", self.stage)
        self.assertNotIn("COPY deploy/hermes", self.stage)

    def test_gstack_is_fetched_at_a_full_commit_and_the_bundle_checksum_is_verified(self) -> None:
        self.assertIn(f"ARG GSTACK_COMMIT={GSTACK_COMMIT}", self.stage)
        self.assertIn(f"ARG GSTACK_DIAGRAM_BUNDLE_SHA256={GSTACK_BUNDLE_SHA256}", self.stage)
        self.assertRegex(self.stage, r"(?m)^ARG GSTACK_COMMIT=[0-9a-f]{40}$")
        self.assertRegex(self.stage, r"(?m)^ARG GSTACK_DIAGRAM_BUNDLE_SHA256=[0-9a-f]{64}$")
        self.assertIn('fetch -q --depth 1 https://github.com/garrytan/gstack.git "${GSTACK_COMMIT}"', self.stage)
        self.assertIn('test "$(git -C "$gstack_src" rev-parse HEAD)" = "${GSTACK_COMMIT}"', self.stage)
        self.assertIn('--bundle-sha256 "${GSTACK_DIAGRAM_BUNDLE_SHA256}"', self.stage)

    def test_libraries_install_only_from_the_hash_lock_at_build_time(self) -> None:
        self.assertIn("install-libs.py", self.stage)
        self.assertIn("--lock /tmp/writing-build/requirements.lock", self.stage)
        installer = (WRITING / "install-libs.py").read_text(encoding="utf-8")
        for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps"):
            self.assertIn(flag, installer)
        for interpreter in IMAGE_INTERPRETERS:
            self.assertIn(interpreter, installer)
        self.assertNotIn('"--user"', installer)
        # Nothing at runtime: the setup module and entrypoint never install packages.
        for runtime in ("src/hermes_helmet/writing_setup.py", "deploy/hermes/runtime-entrypoint.sh"):
            self.assertNotRegex(_read(runtime), r"pip3? install|npm (install|ci)|apt(-get)? install")

    def test_build_runs_the_bundled_skills_with_every_image_interpreter(self) -> None:
        for interpreter in IMAGE_INTERPRETERS:
            self.assertIn(interpreter, self.stage)
        self.assertIn('"$py" /opt/hermes-helmet/writing/smoke.py documents', self.stage)
        self.assertIn("smoke.py diagram", self.stage)
        smoke = (WRITING / "smoke.py").read_text(encoding="utf-8")
        for script in ("docx_create.py", "docx_read.py", "docx_edit.py", "pptx_create.py", "pptx_read.py", "pptx_edit.py"):
            self.assertIn(script, smoke)
        self.assertIn("--append-notes", smoke)
        self.assertIn("sys.executable", smoke)

    def test_assets_land_root_owned_and_the_user_returns_to_the_worker(self) -> None:
        self.assertIn("chown -R root:root", self.stage)
        self.assertIn("chmod -R a+rX,go-w", self.stage)
        self.assertLess(self.stage.index("USER root"), self.stage.rindex("USER ${HERMES_UID}:${HERMES_GID}"))
        self.assertIn("COPY examples/writing/guidance /opt/hermes-helmet/examples/writing/guidance", self.stage)
        self.assertIn("com.machinewisdom.hermes-helmet.gstack-commit", self.stage)

    def test_no_minified_javascript_or_bundle_is_committed(self) -> None:
        for path in WRITING.rglob("*"):
            if path.is_file():
                self.assertNotIn(path.suffix, {".html", ".map"}, path)
                self.assertNotIn(".min.", path.name, path)
                self.assertLess(path.stat().st_size, 120_000, f"{path} is too large to be hand-written source")
        for path in (WRITING / "skills" / "diagram" / "scripts").glob("*.mjs"):
            self.assertGreater(len(path.read_text(encoding="utf-8").splitlines()), 40, "adapter must stay readable source")


class WritingImageProvenanceTests(unittest.TestCase):
    def test_lockfile_is_fully_pinned_and_hashed_for_the_declared_inputs(self) -> None:
        lock = _read("deploy/writing/requirements.lock")
        inputs = [
            line.split("==")[0].lower().replace("_", "-")
            for line in _read("deploy/writing/requirements.in").splitlines()
            if line and not line.startswith("#")
        ]
        pinned = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+) \\$", lock)
        names = {name.lower().replace("_", "-") for name, _ in pinned}
        for required in inputs:
            self.assertIn(required, names)
        self.assertTrue({"lxml", "pillow", "python-docx", "python-pptx", "typing-extensions", "xlsxwriter"} <= names)
        for block in re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", lock)[1:]:
            self.assertIn("--hash=sha256:", block, block.splitlines()[0])
        self.assertNotIn("git+", lock)
        self.assertNotIn("http://", lock)

    def test_notice_records_the_pinned_commit_checksum_and_actual_bundle_dependencies(self) -> None:
        notice = _read("deploy/writing/notices/diagram-bundle.md")
        self.assertIn(GSTACK_COMMIT, notice)
        self.assertIn(GSTACK_BUNDLE_SHA256, notice)
        self.assertIn("MIT", notice)
        self.assertIn("SIL Open Font License", notice)
        deps = json.loads(_read("deploy/writing/bundle-deps.json"))
        self.assertEqual(
            deps,
            {
                "@excalidraw/excalidraw": "0.18.1",
                "@excalidraw/mermaid-to-excalidraw": "2.2.2",
                "mermaid": "11.16.1",
                "react": "18.3.1",
                "react-dom": "18.3.1",
            },
        )
        for package, version in deps.items():
            self.assertRegex(notice, rf"\| {re.escape(package)} \| {re.escape(version)} \| MIT \|")
        # The stale upstream table must be called out, not copied.
        self.assertIn("11.12.2", notice)

    def test_root_notice_names_the_writing_image_components(self) -> None:
        notice = _read("NOTICE")
        for expected in ("python-docx", "python-pptx", "gstack", "Pillow", "lxml", "XlsxWriter", "typing_extensions"):
            self.assertIn(expected, notice)
        self.assertIn("deploy/writing/notices/diagram-bundle.md", notice)

    def test_diagram_skill_supports_only_mermaid_svg_and_png(self) -> None:
        skill = _read("deploy/writing/skills/diagram/SKILL.md")
        adapter = _read("deploy/writing/skills/diagram/scripts/render.mjs")
        self.assertNotIn("__mermaidToExcalidraw", adapter)
        self.assertNotIn("writeFileSync(join(out, slug + \".excalidraw\"", adapter)
        self.assertIn("Only Mermaid\nsource, SVG and PNG are supported", skill)
        self.assertIn("SOURCE_EXTENSIONS", adapter)


class WritingStageDiagramTests(unittest.TestCase):
    """The stager fails closed; a synthetic checkout stands in for gstack."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stager = _load_script("stage_diagram_for_tests", WRITING / "stage-diagram.py")

    def make_gstack(self, root: Path, *, bundle: bytes = b"<html>synthetic</html>", deps=None) -> tuple[Path, str]:
        wanted = deps if deps is not None else json.loads(_read("deploy/writing/bundle-deps.json"))
        dist = root / "lib/diagram-render/dist"
        dist.mkdir(parents=True)
        digest = hashlib.sha256(bundle).hexdigest()
        (dist / "diagram-render.html").write_bytes(bundle)
        (dist / "BUILD_INFO.json").write_text(
            json.dumps({"name": "gstack-diagram-render", "sha256": digest, "srcSha256": "s", "bytes": len(bundle), "bunVersion": "1", "deps": wanted}),
            encoding="utf-8",
        )
        (root / "lib/diagram-render/THIRD-PARTY-LICENSES.md").write_text("licenses\n", encoding="utf-8")
        (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (root / "VERSION").write_text("1.0.0.0\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "fixture")):
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                check=True, capture_output=True,
            )
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        return root, commit

    def stage(self, root: Path, commit: str, sha: str, out: Path):
        return self.stager.stage(root, out, commit=commit, bundle_sha256=sha, owned_root=WRITING)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_stages_the_bundle_notices_adapter_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, commit = self.make_gstack(Path(tmp) / "g")
            out = Path(tmp) / "out" / "diagram"
            result = self.stage(root, commit, hashlib.sha256(b"<html>synthetic</html>").hexdigest(), out)
            for relative in (
                "assets/diagram-render.html", "assets/BUILD_INFO.json", "scripts/render.mjs", "SKILL.md",
                "PROVENANCE.json", "notices/gstack-LICENSE", "notices/gstack-THIRD-PARTY-LICENSES.md",
                "notices/diagram-bundle.md",
            ):
                self.assertTrue((out / relative).is_file(), relative)
            provenance = json.loads((out / "PROVENANCE.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance["gstack"]["commit"], commit)
            self.assertEqual(provenance["bundle"]["sha256"], result["bundle"]["sha256"])
            self.assertEqual(
                provenance["files"]["assets/diagram-render.html"],
                hashlib.sha256((out / "assets/diagram-render.html").read_bytes()).hexdigest(),
            )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_fails_closed_on_every_pin_mismatch(self) -> None:
        digest = hashlib.sha256(b"<html>synthetic</html>").hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root, commit = self.make_gstack(Path(tmp) / "g")
            cases = {
                "short commit": (commit[:12], digest),
                "other commit": ("1" * 40, digest),
                "other checksum": (commit, "0" * 64),
                "short checksum": (commit, digest[:16]),
            }
            for label, (pin, sha) in cases.items():
                with self.subTest(label), self.assertRaises(self.stager.StageError):
                    self.stage(root, pin, sha, Path(tmp) / f"out-{label.replace(' ', '-')}")
            self.assertFalse(any(Path(tmp).glob("out-*")), "a refused stage must leave nothing behind")

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_fails_when_build_info_disagrees_or_dependencies_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, commit = self.make_gstack(Path(tmp) / "g1", deps={"mermaid": "99.0.0"})
            with self.assertRaises(self.stager.StageError) as caught:
                self.stage(root, commit, hashlib.sha256(b"<html>synthetic</html>").hexdigest(), Path(tmp) / "o1")
            self.assertIn("bundle-deps.json", str(caught.exception))
            root, commit = self.make_gstack(Path(tmp) / "g2")
            info = root / "lib/diagram-render/dist/BUILD_INFO.json"
            data = json.loads(info.read_text(encoding="utf-8"))
            data["sha256"] = "f" * 64
            info.write_text(json.dumps(data), encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qam", "drift"],
                check=True, capture_output=True,
            )
            new_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            with self.assertRaises(self.stager.StageError):
                self.stage(root, new_commit, hashlib.sha256(b"<html>synthetic</html>").hexdigest(), Path(tmp) / "o2")


class WritingComposeRecipeTests(unittest.TestCase):
    COMPOSE = ROOT / "deploy" / "writing.compose.yaml"

    def setUp(self) -> None:
        self.text = self.COMPOSE.read_text(encoding="utf-8")
        self.code = re.sub(r"(?m)^\s*#.*$", "", self.text)

    def test_recipe_is_standalone_with_no_global_names_secrets_binds_or_ports(self) -> None:
        for forbidden in ("container_name", "GH_TOKEN", "GITHUB_TOKEN", "HERMES_GITHUB_TOKEN", "policy", "ports:", "poller", "extends:", "include:", "entrypoint", "image:"):
            self.assertNotIn(forbidden, self.code, forbidden)
        services = self.code.split("\nvolumes:")[0].split("services:")[1]
        self.assertEqual(re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", services), ["hermes"])
        self.assertIn("target: writing", self.code)
        self.assertIn("dockerfile: deploy/Dockerfile", self.code)
        self.assertIn('user: "10000:10000"', self.code)
        self.assertIn("HERMES_HOME: /opt/data", self.code)
        self.assertIn("HOME: /opt/data", self.code)
        # Only the named volume is mounted: no host path binds.
        mounts = re.findall(r"(?m)^      - ([^\s:]+):/[^\s]+", self.code.split("tmpfs:")[0])
        self.assertEqual(mounts, ["hermes_state"])
        # Neither the volume nor the network carries a global name.
        self.assertNotRegex(self.code.split("\nvolumes:")[1], r"(?m)^\s+name:")
        self.assertNotIn("networks:", self.code)

    def test_base_compose_file_is_unchanged_in_its_global_contract(self) -> None:
        base = _read("deploy/compose.yaml")
        self.assertIn("container_name: hermes-helmet", base)
        self.assertIn("name: hermes_helmet_state", base)
        self.assertIn("HERMES_GITHUB_TOKEN:?", base)

    @unittest.skipUnless(shutil.which("docker"), "docker with the compose plugin is required")
    def test_two_projects_resolve_to_distinct_resources_with_no_github_token(self) -> None:
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("docker compose plugin is not available")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "COMPOSE_", "GH_", "GITHUB_"))}
        resolved = {}
        for project in ("writing-one", "writing-two"):
            done = subprocess.run(
                ["docker", "compose", "-f", str(self.COMPOSE), "-p", project, "config", "--format", "json"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            resolved[project] = json.loads(done.stdout)
        names = {}
        for project, config in resolved.items():
            self.assertEqual(list(config["services"]), ["hermes"])
            service = config["services"]["hermes"]
            self.assertNotIn("container_name", service)
            self.assertNotIn("ports", service)
            self.assertEqual(service["user"], "10000:10000")
            self.assertEqual(service["build"]["target"], "writing")
            self.assertEqual(service["environment"]["HERMES_HOME"], "/opt/data")
            self.assertTrue(all(m["type"] == "volume" for m in service["volumes"]))
            self.assertFalse(any("TOKEN" in key for key in service["environment"]))
            names[project] = {
                "volume": config["volumes"]["hermes_state"]["name"],
                "network": config["networks"]["default"]["name"],
            }
            self.assertTrue(names[project]["volume"].startswith(project))
            self.assertTrue(names[project]["network"].startswith(project))
        self.assertNotEqual(names["writing-one"]["volume"], names["writing-two"]["volume"])
        self.assertNotEqual(names["writing-one"]["network"], names["writing-two"]["network"])
        for resources in names.values():
            self.assertNotIn("hermes_helmet_state", resources["volume"])
            self.assertNotIn("hermes_helmet_private", resources["network"])


@unittest.skipUnless(shutil.which("node"), "Node 22+ is required")
class WritingDiagramCommandTests(unittest.TestCase):
    """The real diagram command, offline, against a staged real bundle.

    Needs a staged diagram skill: the writing image carries it at
    ``/opt/hermes-helmet/writing``; elsewhere set ``HELMET_DIAGRAM_ASSETS`` to a
    directory containing ``skills/diagram``. Skipped otherwise.
    """

    def setUp(self) -> None:
        assets = os.environ.get("HELMET_DIAGRAM_ASSETS") or "/opt/hermes-helmet/writing"
        if not (Path(assets) / "skills" / "diagram" / "assets" / "diagram-render.html").is_file():
            self.skipTest("no staged diagram skill; build the writing image or set HELMET_DIAGRAM_ASSETS")
        self.assets = Path(assets)

    def test_smoke_diagram_passes_offline_with_labels_png_and_retained_source(self) -> None:
        done = subprocess.run(
            [sys.executable, str(WRITING / "smoke.py"), "diagram", "--assets", str(self.assets)],
            capture_output=True, text=True, timeout=240,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        summary = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(summary["ok"])
        self.assertGreater(summary["png"][0], 0)
        self.assertGreater(summary["png"][1], 0)
        self.assertEqual(set(summary["labels"]), {"Intake", "Draft", "Edit", "Publish", "reviewed"})


if __name__ == "__main__":
    unittest.main()
