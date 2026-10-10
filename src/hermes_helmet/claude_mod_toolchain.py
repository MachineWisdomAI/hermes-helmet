#!/usr/bin/env python3
"""Install the accepted Claude Code CLI and check the Bridge mod with it.

The digests below are the SHA-256 values published in the signed release
manifest at ``downloads.claude.ai`` for Claude Code 2.1.296 (they equal the
digests of the matching ``@anthropic-ai/claude-code-<platform>`` npm binaries).
Keeping them beside the version makes a bump an explicit, reviewable
supply-chain change.  Mods are early access, so the verified version is also
recorded in ``docs/first-officer-plugins.md``; re-verify on a minimum-version
change.

``claude plugin validate`` and ``claude plugin test`` need no account
credentials; they run here with an empty, temporary ``HOME``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib import request


CLAUDE_CODE_VERSION = "2.1.296"
# Lowest build the mod is verified on; never lower than 2.1.293 (issue #55).
MINIMUM_CLAUDE_CODE_VERSION = "2.1.293"
RELEASE_ROOT = "https://downloads.claude.ai/claude-code-releases"
ARTIFACTS: dict[str, str] = {
    "linux-x64": "24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de",
    "linux-arm64": "f1f6e96e0d8342b9dbf41d7e88255397a6a52ce3d8736ad6a4c6b59c9b62fefa",
    "darwin-x64": "a6bf4f30be241053a923f3820ad23c5991d2f5ac2935b3a8dd64da218d9cc603",
    "darwin-arm64": "c9b5341637becbd423ddffc5b254afb645682a3868cb708bbc6cc0e7bb419937",
}

# The exact read-only allowlist from issue #55.  A call outside it fails CI.
ALLOWED_CALLS = frozenset(
    {
        "$.session.id",
        "$.session.version",
        "$.process.run",
        "$.model.complete",
        "$.command.register",
        "$.command.list",
        "$.command.run",
        "$.ui.open",
        "$.ui.resolve",
        "$.ui.status",
        "$.ui.toast",
        "$.state.get",
        "$.state.set",
        "$.clock.after",
    }
)
_CALLS_RE = re.compile(r"^(?P<module>\S+) calls: (?P<calls>.+)$")
_CALL_RE = re.compile(r"^\$\.[a-z][A-Za-z]*\.[a-z][A-Za-z]*$")


class ClaudeModToolchainError(RuntimeError):
    """The accepted Claude Code toolchain could not be selected or verified."""


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if match is None:
        raise ClaudeModToolchainError(f"unrecognized Claude Code version: {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def require_version(version: str) -> None:
    if version != CLAUDE_CODE_VERSION:
        raise ClaudeModToolchainError(
            f"Claude Code version drift: expected {CLAUDE_CODE_VERSION}, received {version}"
        )


def require_version_output(output: str) -> None:
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)\s*", output)
    if match is None:
        raise ClaudeModToolchainError("Claude Code version output is unrecognized")
    require_version(match.group(1))


def require_minimum(version: str) -> None:
    if _version_tuple(version) < _version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
        raise ClaudeModToolchainError(
            f"Claude Code {version} is below the minimum {MINIMUM_CLAUDE_CODE_VERSION}"
        )


def artifact_platform(*, system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).casefold()
    machine_name = (machine or platform.machine()).casefold()
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine_name)
    if system_name not in {"darwin", "linux"} or architecture is None:
        raise ClaudeModToolchainError(
            f"unsupported Claude Code platform: {system_name}/{machine_name}"
        )
    name = f"{system_name}-{architecture}"
    if name not in ARTIFACTS:
        raise ClaudeModToolchainError(f"unsupported Claude Code artifact: {name}")
    return name


def verify_sha256(payload: bytes, expected: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ClaudeModToolchainError(
            f"Claude Code checksum mismatch: expected {expected}, received {actual}"
        )


def _clean_env(home: str) -> dict[str, str]:
    """An environment with no account credentials, in a throwaway home."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH"))
        and key not in {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}
    }
    env["HOME"] = home
    env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    return env


def validate_binary(binary: Path) -> None:
    with tempfile.TemporaryDirectory() as home:
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
                env=_clean_env(home),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClaudeModToolchainError("Claude Code version check failed") from exc
    require_version_output(completed.stdout)


def install(*, bin_dir: Path, version: str = CLAUDE_CODE_VERSION) -> Path:
    require_version(version)
    name = artifact_platform()
    url = f"{RELEASE_ROOT}/{version}/{name}/claude"
    try:
        with request.urlopen(url, timeout=300) as response:
            payload = response.read()
    except OSError as exc:
        raise ClaudeModToolchainError("Claude Code release asset download failed") from exc
    verify_sha256(payload, ARTIFACTS[name])

    bin_dir = bin_dir.expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    destination = bin_dir / "claude"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".claude.", dir=bin_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return destination


_VIA = r"(?: \(via [A-Za-z_$][\w$]*(?:, [A-Za-z_$][\w$]*)*\))?"
_CALL_ENTRY = rf"\$\.[a-z][A-Za-z]*\.[a-z][A-Za-z]*{_VIA}"
_CALL_LIST_RE = re.compile(rf"{_CALL_ENTRY}(?:, {_CALL_ENTRY})*")


def _split_calls(text: str) -> list[str]:
    """Split a ``calls:`` list, dropping the ``(via helper, ...)`` notes.

    ``claude plugin validate`` annotates a call made inside a helper function
    with that helper's name.  Only that exact annotation is accepted; anything
    else in the list is unparseable (an empty result), so every call name is
    still checked against the allowlist.
    """

    if _CALL_LIST_RE.fullmatch(text) is None:
        return []
    return re.findall(r"\$\.[a-z][A-Za-z]*\.[a-z][A-Za-z]*", text)


def calls_from_validation(report_text: str) -> dict[str, list[str]]:
    """Return the ``calls:`` each hooks module makes, per ``claude plugin validate --json``.

    Fails closed: unparseable output, a missing hooks module, a missing
    ``calls:`` line, or a malformed call name raises.
    """

    try:
        report = json.loads(report_text)
    except (TypeError, ValueError) as exc:
        raise ClaudeModToolchainError("validation output is not JSON") from exc
    if not isinstance(report, dict) or report.get("success") is not True:
        raise ClaudeModToolchainError("validation did not succeed")
    contents = report.get("contents")
    if not isinstance(contents, list):
        raise ClaudeModToolchainError("validation output has no contents list")
    found: dict[str, list[str]] = {}
    for entry in contents:
        if not isinstance(entry, dict) or entry.get("type") != "hooks":
            continue
        notes = entry.get("notes")
        if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
            raise ClaudeModToolchainError("validation hooks notes are unparseable")
        for note in notes:
            match = _CALLS_RE.match(note)
            if match is None:
                continue
            names = _split_calls(match.group("calls"))
            if not names or any(_CALL_RE.match(name) is None for name in names):
                raise ClaudeModToolchainError(f"validation calls are unparseable: {note!r}")
            found.setdefault(match.group("module"), []).extend(names)
    if not found:
        raise ClaudeModToolchainError("validation listed no hooks module calls")
    return found


def forbidden_calls(found: dict[str, list[str]]) -> list[str]:
    return sorted({call for calls in found.values() for call in calls} - ALLOWED_CALLS)


def check_mod_calls(report_text: str) -> list[str]:
    """Return the sorted calls the mod makes; raise if any is outside the allowlist."""

    found = calls_from_validation(report_text)
    forbidden = forbidden_calls(found)
    if forbidden:
        raise ClaudeModToolchainError(
            "mod makes calls outside the read-only allowlist: " + ", ".join(forbidden)
        )
    return sorted({call for calls in found.values() for call in calls})


def run_mod_checks(binary: Path, plugin_root: Path) -> list[str]:
    """Run the real ``claude plugin validate`` and ``claude plugin test``."""

    validate_binary(binary)
    with tempfile.TemporaryDirectory() as home:
        env = _clean_env(home)
        validated = subprocess.run(
            [str(binary), "plugin", "validate", str(plugin_root), "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            env=env,
            check=False,
        )
        if validated.returncode != 0:
            raise ClaudeModToolchainError(
                "claude plugin validate failed:\n" + validated.stdout[-4000:] + validated.stderr[-2000:]
            )
        calls = check_mod_calls(validated.stdout)
        tested = subprocess.run(
            [str(binary), "plugin", "test", str(plugin_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=900,
            env=env,
            check=False,
        )
    sys.stdout.write(tested.stdout)
    if tested.returncode != 0:
        raise ClaudeModToolchainError("claude plugin test failed")
    if not re.search(r"^\s*\d+ pass\s*$", tested.stdout, re.MULTILINE) or re.search(
        r"^\s*[1-9]\d* fail\s*$", tested.stdout, re.MULTILINE
    ):
        raise ClaudeModToolchainError("claude plugin test output is unparseable or reports failures")
    return calls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install and use the accepted Claude Code CLI")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bin-dir", type=Path, help="install the checked binary here")
    mode.add_argument("--check-binary", type=Path, help="verify the binary's exact version")
    mode.add_argument(
        "--check-mod",
        nargs=2,
        metavar=("BINARY", "PLUGIN_ROOT"),
        help="run claude plugin validate and test, and check the call allowlist",
    )
    parser.add_argument("--version", default=CLAUDE_CODE_VERSION)
    args = parser.parse_args(argv)
    try:
        if args.check_binary is not None:
            require_version(args.version)
            validate_binary(args.check_binary)
            print(f"claude {CLAUDE_CODE_VERSION}")
            return 0
        if args.check_mod is not None:
            binary, root = (Path(item) for item in args.check_mod)
            calls = run_mod_checks(binary, root)
            print("mod calls within allowlist: " + ", ".join(calls))
            return 0
        assert args.bin_dir is not None
        destination = install(bin_dir=args.bin_dir, version=args.version)
    except ClaudeModToolchainError as exc:
        print(f"claude mod check failed: {exc}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
