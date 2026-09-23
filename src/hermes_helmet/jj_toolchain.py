#!/usr/bin/env python3
"""Install the accepted Jujutsu release from an authenticated release manifest.

The digests below are the SHA-256 values published by GitHub for the upstream
``jj-vcs/jj`` v0.45.1 release assets.  Keeping them beside the version makes a
version bump an explicit, reviewable supply-chain change.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib import request


JJ_VERSION = "0.45.1"
RELEASE_ROOT = f"https://github.com/jj-vcs/jj/releases/download/v{JJ_VERSION}"
ARTIFACTS: dict[str, tuple[str, str]] = {
    "aarch64-apple-darwin": (
        f"jj-v{JJ_VERSION}-aarch64-apple-darwin.tar.gz",
        "51ba42e3d0682616f6eb015045bfe45289b396f03511f9897f645ce8e9272743",
    ),
    "x86_64-apple-darwin": (
        f"jj-v{JJ_VERSION}-x86_64-apple-darwin.tar.gz",
        "6171582d0b5a98a1005cd9643faebff7936812ec264d7968a39d9cef3654a99b",
    ),
    "aarch64-unknown-linux-musl": (
        f"jj-v{JJ_VERSION}-aarch64-unknown-linux-musl.tar.gz",
        "7349a43dd5a20dbc998b10114daa0ee63d2ab863fb822c7eb6b0ebca5903cc69",
    ),
    "x86_64-unknown-linux-musl": (
        f"jj-v{JJ_VERSION}-x86_64-unknown-linux-musl.tar.gz",
        "f35438350b5d61963aac5dd74ede510b31d6b9690769d1a6268cf058cc825f72",
    ),
}


class JjToolchainError(RuntimeError):
    """The accepted Jujutsu toolchain could not be selected or verified."""


def require_version(version: str) -> None:
    if version != JJ_VERSION:
        raise JjToolchainError(
            f"Jujutsu version drift: expected {JJ_VERSION}, received {version}"
        )


def require_version_output(output: str) -> None:
    match = re.fullmatch(r"jj ([0-9]+\.[0-9]+\.[0-9]+)(?:[-+][^\s]+)?\s*", output)
    if match is None:
        raise JjToolchainError("Jujutsu version output is unrecognized")
    require_version(match.group(1))


def validate_binary(binary: Path) -> None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JjToolchainError("Jujutsu version check failed") from exc
    require_version_output(completed.stdout)


def artifact_triple(*, system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).casefold()
    machine_name = (machine or platform.machine()).casefold()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(machine_name)
    operating_system = {
        "darwin": "apple-darwin",
        "linux": "unknown-linux-musl",
    }.get(system_name)
    if architecture is None or operating_system is None:
        raise JjToolchainError(
            f"unsupported Jujutsu platform: {system_name}/{machine_name}"
        )
    triple = f"{architecture}-{operating_system}"
    if triple not in ARTIFACTS:
        raise JjToolchainError(f"unsupported Jujutsu artifact triple: {triple}")
    return triple


def verify_sha256(payload: bytes, expected: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise JjToolchainError(
            f"Jujutsu checksum mismatch: expected {expected}, received {actual}"
        )


def _binary_from_archive(payload: bytes) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            candidates = [
                member
                for member in archive.getmembers()
                if member.isfile() and Path(member.name).name == "jj"
            ]
            if len(candidates) != 1:
                raise JjToolchainError("Jujutsu archive must contain exactly one jj binary")
            member = candidates[0]
            if member.name != "jj" and Path(member.name).parent != Path("."):
                raise JjToolchainError("Jujutsu archive contains an unexpected binary path")
            stream = archive.extractfile(member)
            if stream is None:
                raise JjToolchainError("Jujutsu binary could not be read from archive")
            return stream.read()
    except (tarfile.TarError, OSError) as exc:
        raise JjToolchainError("Jujutsu archive is invalid") from exc


def install(*, bin_dir: Path, version: str = JJ_VERSION) -> Path:
    require_version(version)
    triple = artifact_triple()
    filename, expected_digest = ARTIFACTS[triple]
    url = f"{RELEASE_ROOT}/{filename}"
    try:
        with request.urlopen(url, timeout=60) as response:
            payload = response.read()
    except OSError as exc:
        raise JjToolchainError("Jujutsu release asset download failed") from exc
    verify_sha256(payload, expected_digest)
    binary = _binary_from_archive(payload)

    bin_dir = bin_dir.expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    destination = bin_dir / "jj"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".jj.", dir=bin_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(binary)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the accepted checked Jujutsu binary")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bin-dir", type=Path)
    mode.add_argument("--check-binary", type=Path)
    parser.add_argument("--version", default=JJ_VERSION)
    args = parser.parse_args(argv)
    try:
        if args.check_binary is not None:
            require_version(args.version)
            validate_binary(args.check_binary)
            print(f"jj {JJ_VERSION}")
            return 0
        assert args.bin_dir is not None
        destination = install(bin_dir=args.bin_dir, version=args.version)
    except JjToolchainError as exc:
        print(f"jj install failed: {exc}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
