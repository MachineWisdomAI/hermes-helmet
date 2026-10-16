"""Focused coverage for the Captain-selected Version-Override in mw-version.py."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

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

    def test_override_with_no_affected_unit_is_refused(self):
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "empty")
        code, _, err = self.policy("check", "Version-Override: 0.23.1\n")
        self.assertEqual(code, 1)
        self.assertIn("Version-Override", err)


if __name__ == "__main__":
    unittest.main()
