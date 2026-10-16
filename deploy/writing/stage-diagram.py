#!/usr/bin/env python3
"""Stage the offline ``diagram`` skill from a pinned gstack checkout.

Run by ``deploy/Dockerfile`` (and by the tests) with explicit inputs only:

    stage-diagram.py --gstack DIR --commit SHA40 --bundle-sha256 HEX64 \
        --skill-source deploy/writing/skills/diagram --out DESTINATION

The gstack checkout is the vendor source of the committed Mermaid render bundle.
This script fails closed unless:

- the checkout's ``HEAD`` is exactly the full pinned commit;
- the bundle's SHA-256 equals both the pinned value and gstack's own
  ``BUILD_INFO.json`` record;
- the dependency versions in ``BUILD_INFO.json`` equal ``bundle-deps.json``,
  the list the committed notice (``notices/diagram-bundle.md``) was checked
  against, so a bundle bump cannot ship a stale notice.

It copies the bundle, build metadata and upstream notices beside the reusable
render adapter and records ``PROVENANCE.json``. Nothing is downloaded here and
no minified JavaScript is committed to this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# (path inside the gstack checkout, path inside the staged skill)
VENDOR_FILES = (
    ("lib/diagram-render/dist/diagram-render.html", "assets/diagram-render.html"),
    ("lib/diagram-render/dist/BUILD_INFO.json", "assets/BUILD_INFO.json"),
    ("lib/diagram-render/THIRD-PARTY-LICENSES.md", "notices/gstack-THIRD-PARTY-LICENSES.md"),
    ("LICENSE", "notices/gstack-LICENSE"),
)
# (path inside the repository's deploy/writing, path inside the staged skill)
OWNED_FILES = (
    ("skills/diagram/SKILL.md", "SKILL.md"),
    ("skills/diagram/scripts/render.mjs", "scripts/render.mjs"),
    ("notices/diagram-bundle.md", "notices/diagram-bundle.md"),
)


class StageError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_file(root: Path, relative: str) -> Path:
    walk = root
    for part in Path(relative).parts:
        walk = walk / part
        if walk.is_symlink():
            raise StageError(f"source must not be a symlink: {relative}")
    if not walk.is_file():
        raise StageError(f"source file is missing: {relative}")
    return walk


def stage(
    gstack: Path,
    destination: Path,
    *,
    commit: str,
    bundle_sha256: str,
    owned_root: Path = HERE,
    expected_deps: dict[str, str] | None = None,
) -> dict:
    if not COMMIT_RE.match(commit):
        raise StageError("the gstack pin must be a full 40-character commit")
    if not SHA256_RE.match(bundle_sha256):
        raise StageError("the bundle checksum must be a full SHA-256 hex digest")
    gstack = gstack.resolve()
    if not (gstack / ".git").exists():
        raise StageError(f"gstack source is not a git checkout: {gstack}")
    head = _git(gstack, "rev-parse", "HEAD")
    if head != commit:
        raise StageError(f"gstack HEAD {head} is not the pinned commit {commit}")
    if destination.exists() or destination.is_symlink():
        raise StageError(f"staged skill already exists: {destination}")

    vendor = [(_real_file(gstack, src), rel) for src, rel in VENDOR_FILES]
    owned = [(_real_file(owned_root, src), rel) for src, rel in OWNED_FILES]
    bundle, info_path = vendor[0][0], vendor[1][0]
    info = json.loads(info_path.read_text(encoding="utf-8"))
    actual = _sha256(bundle)
    if actual != bundle_sha256:
        raise StageError("diagram-render.html does not match the pinned SHA-256")
    if info.get("sha256") != actual:
        raise StageError("diagram-render.html does not match the sha256 in BUILD_INFO.json")
    wanted = (
        expected_deps
        if expected_deps is not None
        else json.loads((owned_root / "bundle-deps.json").read_text(encoding="utf-8"))
    )
    if info.get("deps") != wanted:
        raise StageError(
            "BUILD_INFO.json dependencies differ from deploy/writing/bundle-deps.json; "
            "re-check notices/diagram-bundle.md against the bundle before updating the pin"
        )

    for source, rel in [*vendor, *owned]:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    provenance = {
        "skill": "diagram",
        "gstack": {
            "remote": "https://github.com/garrytan/gstack",
            "commit": head,
            "version": (gstack / "VERSION").read_text(encoding="utf-8").strip()
            if (gstack / "VERSION").is_file()
            else None,
        },
        "bundle": {
            "sha256": actual,
            **{key: info.get(key) for key in ("name", "srcSha256", "bytes", "bunVersion", "deps")},
        },
        "files": {rel: _sha256(destination / rel) for _, rel in sorted([*vendor, *owned], key=lambda i: i[1])},
    }
    (destination / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage the offline diagram skill from a pinned gstack checkout.")
    parser.add_argument("--gstack", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--owned-root", type=Path, default=HERE)
    args = parser.parse_args(argv)
    try:
        result = stage(
            args.gstack,
            args.out,
            commit=args.commit,
            bundle_sha256=args.bundle_sha256,
            owned_root=args.owned_root,
        )
    except (StageError, subprocess.CalledProcessError, ValueError, OSError) as exc:
        sys.stderr.write(f"stage-diagram: {exc}\n")
        return 1
    print(f"Staged diagram from gstack {result['gstack']['commit']} (bundle {result['bundle']['sha256'][:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
