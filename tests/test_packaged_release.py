#!/usr/bin/env python3
"""Distributable packaging coverage for the public release-candidate gate.

Builds an sdist, a wheel from the unpacked sdist, and installs that wheel into
a clean venv outside the checkout. This is a test/CI helper, not a runtime
framework.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
BUNDLED_PREFIX = "hermes_helmet/bundled_skills/"
INCIDENTAL_NAMES = {"__pycache__", ".DS_Store"}
INCIDENTAL_SUFFIXES = {".pyc", ".pyo"}
OMITTED_REFERENCE = Path("helmet-issue") / "references" / "delivery.md"


def _is_incidental(path: Path) -> bool:
    if path.suffix in INCIDENTAL_SUFFIXES:
        return True
    return any(part in INCIDENTAL_NAMES for part in path.parts)


def source_skill_assets(skills_root: Path = SKILLS_ROOT) -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    for path in sorted(skills_root.rglob("*")):
        if not path.is_file() or _is_incidental(path):
            continue
        assets[path.relative_to(skills_root).as_posix()] = path.read_bytes()
    return assets


def wheel_skill_assets(wheel: Path) -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.startswith(BUNDLED_PREFIX) or name.endswith("/"):
                continue
            relative = name[len(BUNDLED_PREFIX) :]
            path = Path(relative)
            if _is_incidental(path):
                continue
            assets[relative] = archive.read(name)
    return assets


def asset_diff(
    expected: dict[str, bytes], actual: dict[str, bytes]
) -> tuple[list[str], list[str], list[str]]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        name for name in set(expected) & set(actual) if expected[name] != actual[name]
    )
    return missing, extra, mismatched


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_console(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("HERMES_HELMET_")
    }
    env["PYTHONPATH"] = ""
    env.update(extra)
    return env


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env=_clean_env() if env is None else env,
    )


def _require_ok(completed: subprocess.CompletedProcess[str], label: str) -> None:
    if completed.returncode != 0:
        raise AssertionError(
            f"{label} failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}"
        )


def _bootstrap_build_python(venv_dir: Path) -> Path:
    venv.create(venv_dir, with_pip=True, clear=True)
    python = _venv_python(venv_dir)
    bootstrap = _run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        cwd=venv_dir,
    )
    _require_ok(bootstrap, "build toolchain install")
    return python


def _build_sdist(build_python: Path, dist: Path) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            str(build_python),
            "-c",
            "from setuptools.build_meta import build_sdist; "
            f"print(build_sdist({str(dist)!r}))",
        ],
        cwd=ROOT,
    )
    _require_ok(completed, "sdist build")
    sdists = sorted(dist.glob("hermes_helmet-*.tar.gz")) + sorted(
        dist.glob("hermes-helmet-*.tar.gz")
    )
    if len(sdists) != 1:
        raise AssertionError(f"expected one sdist in {list(dist.iterdir())}")
    return sdists[0]


def _unpack_sdist(sdist: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise AssertionError(f"expected one unpacked sdist root in {list(destination.iterdir())}")
    return roots[0]


def _wheel_from_unpacked(build_python: Path, unpacked: Path, dist: Path) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            str(build_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(dist),
            str(unpacked),
        ],
        cwd=unpacked,
    )
    _require_ok(completed, f"wheel from unpacked sdist {unpacked}")
    wheels = sorted(dist.glob("hermes_helmet-*.whl")) + sorted(dist.glob("hermes-helmet-*.whl"))
    if not wheels:
        raise AssertionError(f"no wheel in {list(dist.iterdir())}")
    return wheels[0]


def _install_wheel(python: Path, wheel: Path, cwd: Path) -> None:
    completed = _run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=cwd,
    )
    _require_ok(completed, f"pip install {wheel.name}")


def _write_packaging_fakes(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hermes_helmet_packaging_fakes.py").write_text(
        "import hermes_helmet.doctor as helmet_doctor\n"
        "import hermes_helmet.model_lanes as model_lanes\n"
        "import hermes_helmet.setup as helmet_setup\n"
        "\n"
        "TOKEN = 'github_pat_' + ('0' * 40)\n"
        "\n"
        "class FakeGitHub:\n"
        "    def __init__(self):\n"
        "        self.login = 'example-agent'\n"
        "        self.repos = {\n"
        "            'example-org/demo-repo': {\n"
        "                'permissions': {\n"
        "                    'metadata': 'read',\n"
        "                    'contents': 'write',\n"
        "                    'pull_requests': 'write',\n"
        "                    'issues': 'write',\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        self.labels = {'example-org/demo-repo': {'ready-for-agent', 'hermes-kanban-go'}}\n"
        "        self.mutated = False\n"
        "\n"
        "    def current_user(self, token):\n"
        "        if not token:\n"
        "            raise RuntimeError('github token is missing')\n"
        "        return self.login\n"
        "\n"
        "    def repository_access(self, token, slug):\n"
        "        if slug not in self.repos:\n"
        "            raise RuntimeError('repository is not accessible')\n"
        "        return dict(self.repos[slug])\n"
        "\n"
        "    def list_accessible_repository_slugs(self, token):\n"
        "        return tuple(self.repos.keys())\n"
        "\n"
        "    def list_visible_repositories(self, token):\n"
        "        return tuple({'slug': slug, 'private': True} for slug in self.repos)\n"
        "\n"
        "    def list_labels(self, token, slug):\n"
        "        return tuple(sorted(self.labels.get(slug, set())))\n"
        "\n"
        "    def create_label(self, token, slug, name):\n"
        "        self.mutated = True\n"
        "        self.labels.setdefault(slug, set()).add(name)\n"
        "\n"
        "class FakeTransport:\n"
        "    def request(self, method, url, headers=None, body=None, timeout=None):\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}\n"
        "\n"
        "def _github():\n"
        "    return FakeGitHub()\n"
        "\n"
        "def _transport():\n"
        "    return FakeTransport()\n"
        "\n"
        "def _secret(prompt='GitHub worker PAT: '):\n"
        "    if 'provider' in prompt.casefold():\n"
        "        return 'provider-test-key'\n"
        "    return TOKEN\n"
        "\n"
        "helmet_setup.default_github_client = _github\n"
        "helmet_setup.default_transport = _transport\n"
        "helmet_setup.prompt_secret = _secret\n"
        "helmet_doctor.default_github_client = _github\n"
        "model_lanes.HttpOpenAITransport = FakeTransport\n",
        encoding="utf-8",
    )
    (directory / "sitecustomize.py").write_text(
        "import hermes_helmet_packaging_fakes  # noqa: F401\n",
        encoding="utf-8",
    )
    return directory


def _init_checkout(path: Path, slug: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/{slug}.git"],
        check=True,
    )


def _write_fake_gh(path: Path, issue_url: str) -> None:
    issue_json = json.dumps(
        {
            "number": 1,
            "title": "packaged status",
            "body": "read-only",
            "state": "open",
            "html_url": issue_url,
            "labels": [{"name": "ready-for-agent"}],
        }
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"ISSUE = {issue_json!r}\n"
        "args = sys.argv[1:]\n"
        "if not args or args[0] != 'api':\n"
        "    sys.exit(2)\n"
        "endpoint = args[-1]\n"
        "if endpoint.endswith('/issues/1'):\n"
        "    print(ISSUE); sys.exit(0)\n"
        "if '/timeline' in endpoint or '/pulls' in endpoint:\n"
        "    print('[]'); sys.exit(0)\n"
        "if endpoint == 'user':\n"
        "    print(json.dumps({'login': 'example-captain'})); sys.exit(0)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _answers(work: Path) -> dict[str, object]:
    checkout = work / "repos" / "demo-repo"
    return {
        "company": {"display_name": "ExampleCo", "slug": "exampleco"},
        "captain_github_login": "example-captain",
        "worker_github_login": "example-agent",
        "schedule": "every 15m",
        "board": "default",
        "assignee": "builder",
        "inference_provider": "openai",
        "inference_model": "gpt-4.1",
        "worker_max_turns": 100,
        "ready_label": "ready-for-agent",
        "dispatch_label": "hermes-kanban-go",
        "github_owners": ["example-org"],
        "repositories": [{"slug": "example-org/demo-repo", "worktree": str(checkout)}],
        "openviking": {"selected": False, "confirmed": False},
        "fava_trails": {"selected": False, "confirmed": False},
        "matt_pocock_skills": {"accepted": False},
        "gstack": {"accepted": False},
        "company_skill_pack": {"declined": True},
    }


class PackagedReleaseProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="helmet-packaged-")
        work = Path(cls._tmp.name) / "outside-checkout"
        work.mkdir()
        cls.work = work
        cls.expected = source_skill_assets()
        if "setup-helmet/SKILL.md" not in cls.expected:
            raise AssertionError("source skills tree is missing setup-helmet")
        if OMITTED_REFERENCE.as_posix() not in cls.expected:
            raise AssertionError(f"source skills tree is missing {OMITTED_REFERENCE}")
        build_python = _bootstrap_build_python(work / "build-venv")
        cls.sdist = _build_sdist(build_python, work / "dist")
        unpacked = _unpack_sdist(cls.sdist, work / "unpacked-good")
        cls.wheel = _wheel_from_unpacked(build_python, unpacked, work / "wheel-good")
        cls.build_python = build_python
        install_venv = work / "install-venv"
        venv.create(install_venv, with_pip=True, clear=True)
        cls.install_python = _venv_python(install_venv)
        cls.helmet = _venv_console(install_venv, "helmet")
        cls.legacy = _venv_console(install_venv, "hermes-helmet")
        _install_wheel(cls.install_python, cls.wheel, work)
        cls.fakes = _write_packaging_fakes(work / "cli-fakes")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_wheel_from_sdist_contains_every_skill_and_reference(self) -> None:
        self.assertIn("0.1.0rc1", self.wheel.name)
        missing, extra, mismatched = asset_diff(self.expected, wheel_skill_assets(self.wheel))
        self.assertEqual(missing, [], msg="wheel omitted bundled skill assets")
        self.assertEqual(extra, [], msg="wheel added unexpected bundled skill assets")
        self.assertEqual(mismatched, [], msg="wheel content drifted from source skills/")

    def test_installed_consoles_setup_doctor_status_and_skills(self) -> None:
        run_cwd = self.work / "run-cwd"
        run_cwd.mkdir(exist_ok=True)
        env = _clean_env()
        fake_env = _clean_env(PYTHONPATH=str(self.fakes))

        provenance = _run(
            [
                str(self.install_python),
                "-c",
                (
                    "import hermes_helmet\n"
                    "from pathlib import Path\n"
                    f"root = Path({str(ROOT)!r}).resolve()\n"
                    "pkg = Path(hermes_helmet.__file__).resolve()\n"
                    "assert 'site-packages' in pkg.parts, pkg\n"
                    "assert root not in pkg.parents, pkg\n"
                    "print(pkg)\n"
                ),
            ],
            cwd=run_cwd,
            env=env,
        )
        _require_ok(provenance, "installed package provenance")

        help_short = _run([str(self.helmet), "--help"], cwd=run_cwd, env=env)
        help_legacy = _run([str(self.legacy), "--help"], cwd=run_cwd, env=env)
        _require_ok(help_short, "helmet --help")
        self.assertEqual(help_legacy.returncode, help_short.returncode)
        self.assertEqual(help_legacy.stdout, help_short.stdout)
        self.assertEqual(help_legacy.stderr, help_short.stderr)
        self.assertIn("setup", help_short.stdout)
        self.assertIn("doctor", help_short.stdout)
        self.assertIn("status", help_short.stdout)

        bundled = _run(
            [
                str(self.install_python),
                "-c",
                (
                    "from pathlib import Path\n"
                    "import hermes_helmet, json\n"
                    "root = Path(hermes_helmet.__file__).resolve().parent / 'bundled_skills'\n"
                    "assets = {}\n"
                    "for path in sorted(root.rglob('*')):\n"
                    "    if path.is_file() and path.suffix not in {'.pyc', '.pyo'} "
                    "and '__pycache__' not in path.parts and path.name != '.DS_Store':\n"
                    "        assets[path.relative_to(root).as_posix()] = path.read_bytes().hex()\n"
                    "print(json.dumps(assets))\n"
                ),
            ],
            cwd=run_cwd,
            env=env,
        )
        _require_ok(bundled, "installed bundled_skills listing")
        installed_assets = {
            name: bytes.fromhex(payload)
            for name, payload in json.loads(bundled.stdout).items()
        }
        missing, extra, mismatched = asset_diff(self.expected, installed_assets)
        self.assertEqual((missing, extra, mismatched), ([], [], []))

        prefix = self.work / "skill-prefix"
        prefix.mkdir(exist_ok=True)
        install_skills = _run(
            [str(self.helmet), "install-skills", "--prefix", str(prefix)],
            cwd=run_cwd,
            env=env,
        )
        _require_ok(install_skills, "helmet install-skills")
        host_roots = (
            prefix / ".codex" / "skills",
            prefix / ".claude" / "skills",
            prefix / ".hermes" / "skills" / "hermes-helmet",
        )
        for host_root in host_roots:
            host_assets = {
                path.relative_to(host_root).as_posix(): path.read_bytes()
                for path in host_root.rglob("*")
                if path.is_file() and not _is_incidental(path)
            }
            missing, extra, mismatched = asset_diff(self.expected, host_assets)
            self.assertEqual(
                (missing, extra, mismatched),
                ([], [], []),
                msg=f"installed skill tree drifted under {host_root}",
            )

        proof = self.work / "setup-proof"
        proof.mkdir(exist_ok=True)
        answers = _answers(proof)
        checkout = Path(str(answers["repositories"][0]["worktree"]))  # type: ignore[index]
        _init_checkout(checkout, "example-org/demo-repo")
        home = proof / "home"
        home.mkdir()
        answers_path = proof / "answers.json"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        setup = _run(
            [
                str(self.helmet),
                "setup",
                "--answers",
                str(answers_path),
                "--home",
                str(home),
                "--json",
            ],
            cwd=run_cwd,
            env=fake_env,
        )
        _require_ok(setup, "helmet setup")
        setup_payload = json.loads(setup.stdout)
        self.assertTrue(setup_payload.get("ok"), setup_payload)

        doctor = _run(
            [
                str(self.legacy),
                "doctor",
                "--config",
                str(home / ".hermes-helmet" / "policy.json"),
                "--home",
                str(home),
                "--json",
            ],
            cwd=run_cwd,
            env=fake_env,
        )
        _require_ok(doctor, "hermes-helmet doctor")
        doctor_payload = json.loads(doctor.stdout)
        self.assertTrue(doctor_payload.get("ok"), doctor_payload)
        self.assertTrue(doctor_payload.get("bundled_skills", {}).get("ok"), doctor_payload)
        self.assertTrue(doctor_payload.get("company_skills", {}).get("skipped"), doctor_payload)

        issue_url = "https://github.com/example-org/demo-repo/issues/1"
        gh_bin = proof / "fake-gh"
        hermes_bin = proof / "fake-hermes"
        _write_fake_gh(gh_bin, issue_url)
        hermes_bin.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        hermes_bin.chmod(hermes_bin.stat().st_mode | stat.S_IXUSR)
        ledger = proof / "missing-ledger.sqlite3"
        checkpoints = proof / "checkpoints"
        status = _run(
            [
                str(self.helmet),
                "status",
                issue_url,
                "--config",
                str(home / ".hermes-helmet" / "policy.json"),
                "--ledger",
                str(ledger),
                "--checkpoint-dir",
                str(checkpoints),
                "--gh",
                str(gh_bin),
                "--hermes",
                str(hermes_bin),
                "--json",
            ],
            cwd=run_cwd,
            env=env,
        )
        _require_ok(status, "helmet status")
        report = json.loads(status.stdout)
        self.assertEqual(report.get("issue_url"), issue_url)
        self.assertIn("terminal", report)
        self.assertFalse(ledger.exists())
        self.assertFalse(checkpoints.exists())

    def test_missing_bundled_reference_is_rejected(self) -> None:
        isolated = self.work / "negative"
        isolated.mkdir(exist_ok=True)
        unpacked = _unpack_sdist(self.sdist, isolated / "unpacked")
        omitted = unpacked / "skills" / OMITTED_REFERENCE
        self.assertTrue(omitted.is_file(), omitted)
        omitted.unlink()
        broken_wheel = _wheel_from_unpacked(
            self.build_python, unpacked, isolated / "dist"
        )
        missing, extra, mismatched = asset_diff(
            self.expected, wheel_skill_assets(broken_wheel)
        )
        self.assertIn(OMITTED_REFERENCE.as_posix(), missing)
        self.assertEqual(extra, [])
        self.assertEqual(mismatched, [])

        broken_venv = isolated / "install-venv"
        venv.create(broken_venv, with_pip=True, clear=True)
        python = _venv_python(broken_venv)
        _install_wheel(python, broken_wheel, isolated)
        listing = _run(
            [
                str(python),
                "-c",
                (
                    "from pathlib import Path\n"
                    "import hermes_helmet, json\n"
                    "root = Path(hermes_helmet.__file__).resolve().parent / 'bundled_skills'\n"
                    "print(json.dumps(sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())))\n"
                ),
            ],
            cwd=isolated,
        )
        _require_ok(listing, "broken wheel bundled listing")
        names = json.loads(listing.stdout)
        self.assertNotIn(OMITTED_REFERENCE.as_posix(), names)


if __name__ == "__main__":
    unittest.main()
