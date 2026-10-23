"""Focused coverage for the Captain-selected Version-Override in mw-version.py."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mw-version.py"
FEAT = "feat: add something"


def run_policy(repo: Path, *args: str) -> tuple[int, str, str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mw_version_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.main(["--repo", str(repo), *args])
    return code, out.getvalue(), err.getvalue()


class VersionOverrideTests(unittest.TestCase):
    def setUp(self):
        # `--event-file` defaults to $GITHUB_EVENT_PATH. In CI that is the real pull
        # request event, whose body can carry its own Version-Override and would
        # silently replace the body each test supplies.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITHUB_EVENT_PATH", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        (self.repo / ".version-policy.json").write_text(json.dumps({
            "schema_version": 1,
            "units": [{"name": "repo", "paths": ["**"], "files": [{"path": "VERSION", "kind": "text"}]}],
        }))
        (self.repo / "VERSION").write_text("0.23.0\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "chore: base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "change.txt").write_text("x\n")
        self.git("add", "change.txt")
        self.git("commit", "-q", "-m", FEAT)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def body(self, text: str) -> str:
        path = self.repo.parent / (self.repo.name + "-body.txt")
        path.write_text(text)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def policy(self, command: str, body: str | None = None, title: str = FEAT):
        args = [command, "--base", "origin/main", "--title", title]
        if body is not None:
            args += ["--body-file", self.body(body)]
        return run_policy(self.repo, *args)

    def version(self) -> str:
        return (self.repo / "VERSION").read_text().strip()

    def test_default_feature_is_minor_without_override(self):
        self.assertEqual(self.policy("update")[0], 0)
        self.assertEqual(self.version(), "0.24.0")
        self.assertEqual(self.policy("check")[0], 0)

    def test_override_selects_patch_and_update_then_check_pass(self):
        body = "Why.\n\nVersion-Override: 0.23.1\n"
        code, out, err = self.policy("update", body)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.version(), "0.23.1")
        self.assertEqual(self.policy("check", body)[0], 0)

    def test_check_rejects_default_version_when_override_selects_patch(self):
        self.policy("update")
        code, _, err = self.policy("check", "Version-Override: 0.23.1\n")
        self.assertEqual(code, 1)
        self.assertIn("expected 0.23.1", err)

    def test_check_rejects_override_version_without_override_line(self):
        self.policy("update", "Version-Override: 0.23.1\n")
        code, _, err = self.policy("check", "no override here\n")
        self.assertEqual(code, 1)
        self.assertIn("expected 0.24.0", err)

    def test_override_cannot_raise_or_skip_the_increment(self):
        for chosen in ("0.25.0", "1.0.0", "0.23.2", "0.23.0", "0.22.9", "0.24.0-rc.1"):
            with self.subTest(chosen=chosen):
                code, _, err = self.policy("check", f"Version-Override: {chosen}\n")
                self.assertEqual(code, 1)
                self.assertIn("Version-Override", err)

    def test_override_matching_default_minor_is_allowed(self):
        self.assertEqual(self.policy("update", "Version-Override: 0.24.0\n")[0], 0)
        self.assertEqual(self.version(), "0.24.0")

    def test_malformed_and_duplicate_overrides_fail(self):
        for body in ("Version-Override:\n", "Version-Override: nope\n",
                     "Version-Override: 0.23.1\nVersion-Override: 0.23.1\n"):
            with self.subTest(body=body):
                code, _, err = self.policy("check", body)
                self.assertEqual(code, 1)
                self.assertIn("Version-Override", err)

    def test_override_only_read_from_pr_body_not_commit_messages(self):
        (self.repo / "more.txt").write_text("y\n")
        self.git("add", "more.txt")
        self.git("commit", "-q", "-m", "fix: more\n\nVersion-Override: 0.23.1")
        self.assertEqual(self.policy("update")[0], 0)
        self.assertEqual(self.version(), "0.24.0")

    def test_override_cannot_lower_breaking_change_or_promotion(self):
        code, _, err = self.policy("check", "BREAKING CHANGE: yes\nVersion-Override: 0.23.1\n")
        self.assertEqual(code, 1)
        self.assertIn("breaking", err)
        code, _, err = self.policy("check", "Version-Override: 0.23.1\n", title="chore(release): promote version")
        self.assertEqual(code, 1)
        self.assertIn("promotion", err)

    def test_graduate_zero_major_to_one_with_version_values_only(self):
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "graduate")
        title = "chore(release): graduate to 1.0.0"
        code, _, err = self.policy("update", title=title)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.version(), "1.0.0")
        self.assertEqual(self.policy("check", title=title)[0], 0)
        (self.repo / "change.txt").write_text("product change\n")
        code, _, err = self.policy("check", title=title)
        self.assertEqual(code, 1)
        self.assertIn("configured version values only", err)

    def test_graduation_rejects_already_stable_major(self):
        (self.repo / "VERSION").write_text("1.2.3\n")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", "chore: stable baseline")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        code, _, err = self.policy("update", title="chore(release): graduate to 1.0.0")
        self.assertEqual(code, 1)
        self.assertIn("zero-major baseline", err)

    def test_override_with_no_affected_unit_is_refused(self):
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "empty")
        code, _, err = self.policy("check", "Version-Override: 0.23.1\n")
        self.assertEqual(code, 1)
        self.assertIn("Version-Override", err)


class ShippedMarkdownVersionTests(unittest.TestCase):
    def test_image_guidance_and_notices_require_a_version_bump(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)

            def git(*args):
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                               check=True, capture_output=True)

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Version test")
            git("config", "user.email", "test@example.invalid")
            config = {
                "schema_version": 1,
                "units": [{"name": "repo", "paths": ["**"],
                           "files": [{"path": "VERSION", "kind": "text"}]}],
                "release_paths": json.loads((ROOT / ".version-policy.json").read_text())["release_paths"],
            }
            (repo / ".version-policy.json").write_text(json.dumps(config))
            (repo / "VERSION").write_text("1.2.3\n")
            git("add", "--", ".version-policy.json", "VERSION")
            git("commit", "-q", "-m", "chore: baseline")
            git("checkout", "-q", "-b", "change")
            for relative in ["examples/writing/guidance/example-writing-guidance/SKILL.md",
                             "deploy/writing/notices/diagram-bundle.md"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic shipped content\n")
                code, _, error = run_policy(repo, "check", "--base", "main", "--title", "docs: update shipped content")
                self.assertEqual(code, 1, error)
                self.assertIn("expected 1.2.4", error)
            code, _, error = run_policy(repo, "update", "--base", "main", "--title", "docs: update shipped content")
            self.assertEqual(code, 0, error)
            self.assertEqual((repo / "VERSION").read_text(), "1.2.4\n")


if __name__ == "__main__":
    unittest.main()
