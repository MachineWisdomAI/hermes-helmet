#!/usr/bin/env python3
"""Install the hash-locked document libraries into every image interpreter.

Build-time only (run by ``deploy/Dockerfile`` as root); never at runtime and never
into a profile user site. The Hermes base image has three interpreters that the
bundled ``docx`` and ``powerpoint`` skills can reach, and none shares a site
directory with another:

- ``/opt/hermes/.venv/bin/python`` -- CPython 3.14, the Hermes virtual environment
  (first on ``PATH``, so a bare ``python`` or ``python3`` in a worker shell);
- ``/usr/local/bin/python3`` -- the CPython 3.14 the environment is built on;
- ``/usr/bin/python3`` -- Debian's CPython 3.13.

Each is given the same locked, hash-verified wheels with
``pip install --require-hashes --only-binary=:all: --no-deps``, using the pip
wheel bundled with the base interpreter's ``ensurepip`` (no network pip, no
``get-pip``). ``--no-deps`` is safe because the lock lists every transitive
dependency. After installing, each interpreter must import the libraries at the
locked versions, or the build fails.

    install-libs.py --lock requirements.lock [--interpreter PATH[=SITE_DIR] ...]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_INTERPRETERS = (
    "/opt/hermes/.venv/bin/python",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
)
# import name -> distribution name in the lock
IMPORTS = {"docx": "python-docx", "pptx": "python-pptx", "PIL": "pillow", "lxml": "lxml"}
LOCKED = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", re.MULTILINE)


class InstallError(Exception):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock: Path) -> dict[str, str]:
    versions = {_norm(name): version for name, version in LOCKED.findall(lock.read_text(encoding="utf-8"))}
    if not versions:
        raise InstallError("the lock names no pinned packages")
    if "--hash=sha256:" not in lock.read_text(encoding="utf-8"):
        raise InstallError("the lock carries no hashes")
    return versions


def _capture(argv: list[str], **kwargs) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
        raise InstallError(f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} failed: {tail[0][:300]}")
    return done.stdout.strip()


def site_directory(interpreter: str) -> Path:
    """The interpreter's own first site directory (not a user site)."""

    program = "import site; print(site.getsitepackages()[0])"
    return Path(_capture([interpreter, "-c", program]))


def bundled_pip_wheel(interpreter: str) -> Path:
    program = (
        "import ensurepip, pathlib; "
        "print(next(pathlib.Path(ensurepip.__file__).parent.glob('_bundled/pip-*.whl')))"
    )
    return Path(_capture([interpreter, "-c", program]))


def install(interpreter: str, site: Path, lock: Path, pip_wheel: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    env_path = str(pip_wheel)
    _capture(
        [
            interpreter, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-compile", "--no-input",
            "--require-hashes", "--only-binary=:all:", "--no-deps",
            "--upgrade", "--target", str(site), "-r", str(lock),
        ],
        env={"PYTHONPATH": env_path, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": tempfile.gettempdir()},
    )
    # ``--target`` also drops package console scripts into ``<site>/bin``.
    shutil.rmtree(site / "bin", ignore_errors=True)


def check(interpreter: str, site: Path, versions: dict[str, str]) -> dict[str, str]:
    """Import every library with ``interpreter`` and compare to the lock."""

    program = (
        "import importlib.metadata as m, json, sys; "
        f"names = {sorted(set(IMPORTS.values()))!r}; "
        f"[__import__(i) for i in {sorted(IMPORTS)!r}]; "
        "print(json.dumps({n: m.version(n) for n in names}))"
    )
    import json

    seen = json.loads(_capture([interpreter, "-c", program], env={"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"}))
    for name, version in seen.items():
        if versions.get(_norm(name)) != version:
            raise InstallError(f"{interpreter}: {name} is {version}, the lock pins {versions.get(_norm(name))}")
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the hash-locked document libraries into each image interpreter.")
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--interpreter", action="append", help="PATH or PATH=SITE_DIR; repeatable")
    args = parser.parse_args(argv)
    try:
        versions = locked_versions(args.lock)
        targets = []
        for spec in args.interpreter or DEFAULT_INTERPRETERS:
            interpreter, _, site = spec.partition("=")
            if not Path(interpreter).is_file():
                raise InstallError(f"interpreter not found: {interpreter}")
            targets.append((interpreter, Path(site) if site else site_directory(interpreter)))
        # One pip wheel (from the first interpreter) serves all: it is pure Python.
        pip_wheel = bundled_pip_wheel(targets[0][0])
        for interpreter, site in targets:
            install(interpreter, site, args.lock, pip_wheel)
            seen = check(interpreter, site, versions)
            print(f"installed {interpreter} -> {site}: " + ", ".join(f"{k} {v}" for k, v in sorted(seen.items())))
    except InstallError as exc:
        sys.stderr.write(f"install-libs: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
