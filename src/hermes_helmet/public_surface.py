#!/usr/bin/env python3
"""Public-surface scan for adopter identities and private runtime defaults.

Forbidden tokens are assembled from parts so this module remains a clean public
surface. Project-provenance citations for this repository and the canonical
FAVA Trails pin are stripped before the remaining text is checked.
"""

from __future__ import annotations

import re
from pathlib import Path


FAVA_PIN = "10f689f7455c0c5c5898f2a2e6bc8cf4fe84a6d7"
_MW = "Machine" + "WisdomAI"
_OPT_DATA = "/opt" + "/data"
_FORBIDDEN = re.compile(
    "|".join(
        (
            "time" + "left--",
            "yia-" + "mw-agent",
            "wisdom" + "helm-builder",
            rf"{_MW}/(www|fava-trails|WisdomHelm)",
            "xai-" + "oauth",
            r"grok-4\.5",
            rf"{_OPT_DATA}/profiles/",
            rf"{_OPT_DATA}/home(?:/|$)",
        )
    )
)
_PINNED_FAVA_PATH = (
    r"(?:/(?:README\.md|AGENTS_SETUP_INSTRUCTIONS\.md|docs/governed-recall\.md))?"
)
_PROVENANCE_END = r"(?![/?#A-Za-z0-9_.-])"
ALLOWED_TOKENS = [
    re.compile(
        rf"https://github\.com/{_MW}/fava-trails/(?:blob|tree)/"
        rf"(?:\{{FAVA_PIN\}}|{FAVA_PIN}){_PINNED_FAVA_PATH}{_PROVENANCE_END}"
    ),
    re.compile(
        rf"github\.com/{_MW}/fava-trails/(?:blob|tree)/"
        rf"(?:\{{FAVA_PIN\}}|{FAVA_PIN}){_PINNED_FAVA_PATH}{_PROVENANCE_END}"
    ),
    re.compile(
        rf"{_MW}/fava-trails/(?:blob|tree)/"
        rf"(?:\{{FAVA_PIN\}}|{FAVA_PIN}){_PINNED_FAVA_PATH}{_PROVENANCE_END}"
    ),
    re.compile(rf"https://github\.com/{_MW}/fava-trails(?![\w./-])"),
    re.compile(rf'CANONICAL_REPO = "https://github\.com/{_MW}/fava-trails"'),
    re.compile(rf'FAVA_PIN = "{FAVA_PIN}"'),
    re.compile(rf"\[FAVA Trails\]\(https://github\.com/{_MW}/fava-trails\)"),
    re.compile(
        rf"Public FAVA pin links are the only {_MW}/fava-trails surface exceptions\."
    ),
    re.compile(rf"https://github\.com/{_MW}/hermes-helmet"),
    re.compile(rf"github\.com/{_MW}/hermes-helmet"),
    re.compile(r"ghcr\.io/machinewisdomai/hermes-helmet"),
    re.compile(r"com\.machinewisdom\.hermes-helmet"),
    re.compile(
        rf"git\+https://github\.com/{_MW}/fava-trails\.git@"
        rf"(?:\$\{{FAVA_PIN\}}|\{{FAVA_PIN\}}|{FAVA_PIN})"
    ),
]
SCAN_ROOTS = (
    "config",
    "src",
    "tests",
    "docs",
    "deploy",
    "skills",
    "scripts",
    ".github",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "AGENTS.md",
    "CLAUDE.md",
    "llms.txt",
    "pyproject.toml",
)
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".zip",
    ".gz",
    ".tgz",
    ".whl",
}


def has_forbidden_public_marker(line: str) -> bool:
    if not _FORBIDDEN.search(line):
        return False
    remaining = line
    for token_re in ALLOWED_TOKENS:
        remaining = token_re.sub("", remaining)
    return bool(_FORBIDDEN.search(remaining))


def scan_public_surface(root: Path) -> list[str]:
    """Return ``path:line:text`` violations for the public tree."""

    violations: list[str] = []
    for root_name in SCAN_ROOTS:
        target = root / root_name
        if not target.exists():
            continue
        paths = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                violations.append(f"{relative}:unreadable-text")
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if has_forbidden_public_marker(line):
                    violations.append(f"{relative}:{lineno}:{line}")
    return violations
