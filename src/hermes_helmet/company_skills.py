#!/usr/bin/env python3
"""Optional company skill-pack import into Hermes-owned persistent state.

Executor packs are adopter-controlled and never write host-global Codex, Claude
Code, or Hermes skill directories. Captain/orchestrator bundled skills install
only through the separate explicit ``install-skills`` setup path.

Import validates the complete candidate before replacing the last known-good
installation. Missing configuration skips cleanly; empty or missing sources
preserve the previous good import.

After a successful import, the active skills directory is registered with the
supported Hermes discovery boundary (``skills.external_dirs``) so executor
skills are visible to the real loader without touching host-global Captain
skill trees.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Sequence

from hermes_helmet.authority import CompanySkillsConfig, Policy


class CompanySkillError(RuntimeError):
    """Company skill pack cannot be validated or imported safely."""


# Executor packs must never collide with Captain/orchestrator skill names.
CAPTAIN_RESERVED_SKILL_NAMES = frozenset(
    {
        "helmet-issue",
        "helmet-epic",
        "setup-helmet",
    }
)

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SKILL_NAME_MAX = 64
ROLE_EXECUTOR = "executor"
ROLE_CAPTAIN = "captain"

# Host-global agent skill trees that runtime import must never target.
HOST_GLOBAL_SKILL_MARKERS = (
    "/.codex/skills",
    "/.claude/skills",
    "/.hermes/skills",
    "\\.codex\\skills",
    "\\.claude\\skills",
    "\\.hermes\\skills",
)

# Credential-shaped tokens: require enough payload after the prefix so ordinary
# skill names like ``task-runner`` (contains ``sk-`` as a substring only when
# written as ``…sk-…`` in free text) are not rejected. Bare ``sk-`` alone is not
# enough; OpenAI/xAI-style keys carry a long identifier after the prefix.
_SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9_-]{20,}"),
)
_AUTHORITY_CLAIM_NEEDLES = (
    "grant review authority",
    "grant merge authority",
    "you may merge pull requests",
    "you may force-push",
    "impersonate the captain and",
    "run as the captain identity",
)

STATUS_SKIPPED = "skipped"
STATUS_IMPORTED = "imported"
STATUS_PRESERVED = "preserved"
STATUS_REJECTED = "rejected"

ACTIVE_NAME = "active"
RELEASES_NAME = "releases"
STAGING_NAME = ".staging"
MANIFEST_NAME = "manifest.json"
SKILLS_DIR_NAME = "skills"


@dataclass(frozen=True)
class SkillCatalogEntry:
    name: str
    role: str
    relative_path: str
    description: str = ""
    source: str = "company_pack"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": self.role,
            "relative_path": self.relative_path,
            "description": self.description,
            "source": self.source,
        }


@dataclass(frozen=True)
class SkillCatalog:
    """Deterministic catalog for setup-helmet and operator inspection."""

    status: str
    role_boundary: str
    skills: tuple[SkillCatalogEntry, ...] = ()
    allowlist: tuple[str, ...] = ()
    source: str | None = None
    state_root: str | None = None
    active_release: str | None = None
    message: str = ""
    generated_at: str = ""
    discovery_path: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "role_boundary": self.role_boundary,
            "skills": [entry.to_public_dict() for entry in self.skills],
            "allowlist": list(self.allowlist),
            "source": self.source,
            "state_root": self.state_root,
            "active_release": self.active_release,
            "message": self.message,
            "generated_at": self.generated_at,
            "discovery_path": self.discovery_path,
        }


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str
    message: str
    selected: tuple[SkillCatalogEntry, ...] = ()
    errors: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "selected": [entry.to_public_dict() for entry in self.selected],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ImportResult:
    status: str
    message: str
    catalog: SkillCatalog
    validation: ValidationResult
    installed_names: tuple[str, ...] = ()
    release_id: str | None = None
    mutated: bool = False
    discovery_path: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "catalog": self.catalog.to_public_dict(),
            "validation": self.validation.to_public_dict(),
            "installed_names": list(self.installed_names),
            "release_id": self.release_id,
            "mutated": self.mutated,
            "discovery_path": self.discovery_path,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _release_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def default_state_root(*, prefix: Path | None = None) -> Path:
    """Hermes Helmet-owned persistent skill state (not host-global agent dirs)."""

    if prefix is not None:
        root = Path(prefix)
    else:
        env = os.environ.get("HERMES_HELMET_STATE_ROOT", "").strip()
        if env:
            root = Path(env)
        else:
            home = os.environ.get("HERMES_HOME", "").strip()
            root = Path(home) if home else Path("/opt/data")
    return (root / "hermes-helmet" / "company-skills").resolve(strict=False)


def assert_not_host_global_skill_path(path: Path, *, field: str = "state_root") -> Path:
    """Reject destinations that land in Codex/Claude/Hermes host skill trees."""

    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CompanySkillError(f"{field}: is not a usable filesystem path") from exc
    text = str(resolved)
    lowered = text.replace("\\", "/").casefold()
    for marker in HOST_GLOBAL_SKILL_MARKERS:
        needle = marker.replace("\\", "/").casefold()
        if needle in lowered:
            raise CompanySkillError(
                f"{field}: runtime company skill import must not use host-global "
                "Codex, Claude Code, or Hermes skill directories"
            )
    return resolved


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def validate_skill_name(name: str) -> str:
    # Do not strip: Hermes loaders keep the literal parsed string, so leading or
    # trailing whitespace is a different identity and must fail the contract.
    text = name if isinstance(name, str) else ""
    if (
        not text
        or text != text.strip()
        or len(text) > SKILL_NAME_MAX
        or not SKILL_NAME_RE.fullmatch(text)
    ):
        raise CompanySkillError(
            "skill name must be a lowercase hyphenated identifier "
            f"(max {SKILL_NAME_MAX} chars)"
        )
    if text in CAPTAIN_RESERVED_SKILL_NAMES:
        raise CompanySkillError(
            "skill name is reserved for Captain/orchestrator bundled skills"
        )
    if ".." in text or "/" in text or "\\" in text:
        raise CompanySkillError("skill name must not contain path elements")
    return text


def _contains_secret_shaped_token(text: str) -> bool:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _frontmatter_body(text: str) -> str:
    """Return the YAML frontmatter body (without opening ---)."""

    if not text.startswith("---"):
        raise CompanySkillError("SKILL.md must start with YAML frontmatter")
    if "\n---\n" not in text[3:]:
        raise CompanySkillError("SKILL.md frontmatter is not closed")
    # Drop the opening --- line; keep content up to the closing fence.
    return text.split("\n---\n", 1)[0][3:].lstrip("\n")


def _decode_double_quoted_yaml(inner: str) -> str:
    """Decode a YAML double-quoted scalar body (escapes already past the quotes)."""

    simple = {
        "0": "\0",
        "a": "\a",
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "v": "\v",
        "f": "\f",
        "r": "\r",
        "e": "\x1b",
        " ": " ",
        '"': '"',
        "/": "/",
        "\\": "\\",
        "N": "\x85",
        "_": "\xa0",
        "L": "\u2028",
        "P": "\u2029",
    }
    out: list[str] = []
    index = 0
    length = len(inner)
    while index < length:
        ch = inner[index]
        if ch != "\\":
            out.append(ch)
            index += 1
            continue
        if index + 1 >= length:
            raise CompanySkillError(
                "unsupported or incomplete YAML escape in quoted scalar"
            )
        nxt = inner[index + 1]
        if nxt in simple:
            out.append(simple[nxt])
            index += 2
            continue
        if nxt == "x":
            hexpart = inner[index + 2 : index + 4]
            if len(hexpart) != 2 or any(
                c not in "0123456789abcdefABCDEF" for c in hexpart
            ):
                raise CompanySkillError(
                    "unsupported or incomplete YAML escape in quoted scalar"
                )
            out.append(chr(int(hexpart, 16)))
            index += 4
            continue
        if nxt == "u":
            hexpart = inner[index + 2 : index + 6]
            if len(hexpart) != 4 or any(
                c not in "0123456789abcdefABCDEF" for c in hexpart
            ):
                raise CompanySkillError(
                    "unsupported or incomplete YAML escape in quoted scalar"
                )
            out.append(chr(int(hexpart, 16)))
            index += 6
            continue
        if nxt == "U":
            hexpart = inner[index + 2 : index + 10]
            if len(hexpart) != 8 or any(
                c not in "0123456789abcdefABCDEF" for c in hexpart
            ):
                raise CompanySkillError(
                    "unsupported or incomplete YAML escape in quoted scalar"
                )
            codepoint = int(hexpart, 16)
            if codepoint > 0x10FFFF:
                raise CompanySkillError(
                    "unsupported or incomplete YAML escape in quoted scalar"
                )
            out.append(chr(codepoint))
            index += 10
            continue
        raise CompanySkillError(
            "unsupported or ambiguous YAML escape in quoted scalar"
        )
    return "".join(out)


def _yaml_block_list_item_scalar(value: str) -> str:
    """Encode ``value`` so a YAML loader keeps it as one list-item string.

    Always emit JSON/YAML double-quoted form so number-looking (``123``,
    ``1.2``) and date-looking (``2026-09-13``) paths stay strings instead of
    int/float/date after rewrite. ``ensure_ascii=False`` keeps non-BMP
    Unicode (for example ``ä🦊``) as the original characters instead of
    JSON surrogate pairs that YAML does not recombine.
    """

    return json.dumps(value, ensure_ascii=False)


def _unquote_yaml_scalar(raw: str) -> str:
    """Remove one layer of YAML single/double quotes from a scalar token.

    Double-quoted escapes match the common YAML set (including ``\\u`` / ``\\U`` /
    ``\\x``) so fallback keys agree with a real loader. Unsupported escapes raise
    rather than silently leaving a different identity. Does not strip leftover
    quote characters from the unquoted value — those are part of the literal
    identity a real YAML loader would expose.
    """

    text = raw.strip()
    if len(text) < 2 or text[0] != text[-1] or text[0] not in "'\"":
        return text
    quote = text[0]
    inner = text[1:-1]
    if quote == "'":
        return inner.replace("''", "'")
    return _decode_double_quoted_yaml(inner)


# PyYAML SafeLoader YAML 1.1 implicit bool/null tokens (quoted scalars stay text).
_YAML11_TRUE = frozenset({"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"})
_YAML11_FALSE = frozenset({"no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"})
_YAML11_NULL = frozenset({"~", "null", "Null", "NULL"})


def _parse_yaml_scalar_value(raw: str) -> object:
    """Resolve a mapping value the way Hermes' YAML loader would.

    Quoted scalars remain strings. Unquoted YAML 1.1 bool/null tokens become
    ``True`` / ``False`` / ``None`` so catalog identity cannot diverge from
    PyYAML. Other plain tokens stay literal strings.
    """

    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return _unquote_yaml_scalar(text)
    if text in _YAML11_TRUE:
        return True
    if text in _YAML11_FALSE:
        return False
    if text in _YAML11_NULL:
        return None
    return _unquote_yaml_scalar(text)


def _normalize_yaml_mapping_key(raw_key: str) -> str:
    """Return the mapping key a YAML loader would use for a plain/quoted key.

    Supported subset is plain identifiers and one-layer quoted scalars (with
    double-quoted escapes). Tags (``!!str name``), anchors, aliases, explicit
    keys, flow indicators, and other token sequences are rejected rather than
    treated as unrelated ordinary fields — a real loader would not keep
    ``!!str name`` as a distinct key from ``name``.
    """

    key = raw_key.strip()
    if not key:
        raise CompanySkillError("SKILL.md frontmatter is malformed")
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
        try:
            normalized = _unquote_yaml_scalar(key)
        except CompanySkillError as exc:
            raise CompanySkillError(
                "SKILL.md frontmatter has unsupported or ambiguous key syntax"
            ) from exc
        if not normalized:
            raise CompanySkillError("SKILL.md frontmatter is malformed")
        # Quoted keys still must not smuggle tag/anchor syntax after unquote.
        if _yaml_key_has_unsupported_token(normalized):
            raise CompanySkillError(
                "SKILL.md frontmatter has unsupported or ambiguous key syntax"
            )
        return normalized
    if key[0] in "'\"" or key[-1] in "'\"":
        raise CompanySkillError(
            "SKILL.md frontmatter has unsupported or ambiguous key syntax"
        )
    if "\\" in key or _yaml_key_has_unsupported_token(key):
        # Plain keys do not carry JSON/YAML escapes; tags/anchors/explicit-key
        # forms cannot be equated to a real loader key without a full parse.
        raise CompanySkillError(
            "SKILL.md frontmatter has unsupported or ambiguous key syntax"
        )
    return key


def _yaml_key_has_unsupported_token(key: str) -> bool:
    """True when a key token is outside the plain/quoted scalar subset."""

    if not key:
        return True
    # Tags (!!str / !local), anchors, aliases, explicit keys, merge, flow.
    if key[0] in "!*&?[{," or any(ch in key for ch in "[]{}:"):
        return True
    if "!" in key or "&" in key or "*" in key:
        return True
    # ``!!str name`` and similar tag+word sequences include whitespace.
    if any(ch.isspace() for ch in key):
        return True
    return False


def _scalar_yaml_text(value: object) -> str:
    """Return the literal string a YAML mapping value already resolved to.

    Do not post-parse strip quotes or surrounding whitespace: loaders that
    already parsed ``name: \"'safe-tool'\"`` expose the literal ``'safe-tool'``,
    and ``name: \" safe-tool \"`` exposes leading/trailing spaces. Both must stay
    distinct from the bare directory identity ``safe-tool``.
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        raise CompanySkillError("SKILL.md frontmatter name/description must be text")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise CompanySkillError("SKILL.md frontmatter name/description must be text")


def _parse_frontmatter_mapping(head: str) -> dict[str, object]:
    """Parse skill frontmatter into a mapping Hermes loaders can consume.

    Prefer PyYAML when available (duplicate keys resolve as the YAML loader does —
    last key wins). Without PyYAML, parse a strict top-level scalar mapping and
    reject duplicate or nested identity fields so identity cannot diverge from the
    directory/catalog boundary. Quoted keys are normalized to the same plain key
    a real loader would use so ``name`` / ``\"name\"`` cannot bypass duplicate
    detection.
    """

    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            loaded = yaml.safe_load(head) if head.strip() else None
        except Exception as exc:  # noqa: BLE001 - surface as validation failure
            raise CompanySkillError("SKILL.md frontmatter is not valid YAML") from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise CompanySkillError("SKILL.md frontmatter must be a YAML mapping")
        return loaded

    mapping: dict[str, object] = {}
    seen_keys: set[str] = set()
    lines = head.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line.startswith(" ") or line.startswith("\t"):
            raise CompanySkillError(
                "SKILL.md frontmatter has nested fields that require a YAML parser"
            )
        if ":" not in line:
            raise CompanySkillError("SKILL.md frontmatter is malformed")
        raw_key, _, rest = line.partition(":")
        key = _normalize_yaml_mapping_key(raw_key)
        if key in seen_keys:
            raise CompanySkillError(
                f"SKILL.md frontmatter has duplicate or ambiguous '{key}' fields"
            )
        seen_keys.add(key)
        value = rest.strip()
        if not value or value in ("|", ">", ">-", "|-"):
            # Block / nested value: identity fields must be plain scalars.
            if key in ("name", "description"):
                raise CompanySkillError(
                    "SKILL.md frontmatter name/description must be a single scalar"
                )
            index += 1
            while index < len(lines) and (
                not lines[index].strip()
                or lines[index].startswith(" ")
                or lines[index].startswith("\t")
            ):
                index += 1
            mapping[key] = ""
            continue
        if value.startswith("[") or value.startswith("{"):
            if key in ("name", "description"):
                raise CompanySkillError(
                    "SKILL.md frontmatter name/description must be a single scalar"
                )
            mapping[key] = value
            index += 1
            continue
        # One YAML quote layer only — leftover quotes stay in the literal value.
        # Unquoted YAML 1.1 bool/null tokens resolve to typed values, matching
        # the real loader rather than remaining catalog strings.
        try:
            mapping[key] = _parse_yaml_scalar_value(value)
        except CompanySkillError as exc:
            if key in ("name", "description"):
                raise CompanySkillError(
                    "SKILL.md frontmatter name/description has unsupported or "
                    "ambiguous scalar syntax"
                ) from exc
            raise CompanySkillError(
                "SKILL.md frontmatter has unsupported or ambiguous scalar syntax"
            ) from exc
        index += 1
    return mapping


def _parse_frontmatter_name_and_description(skill_md: Path) -> tuple[str, str]:
    """Read SKILL.md, enforce UTF-8/frontmatter, return (name, description)."""

    try:
        raw = skill_md.read_bytes()
    except OSError as exc:
        raise CompanySkillError("skill SKILL.md is unreadable") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CompanySkillError("skill SKILL.md is not valid UTF-8") from exc
    head = _frontmatter_body(text)
    if _contains_secret_shaped_token(text):
        raise CompanySkillError("skill appears to contain a secret-shaped token")
    lowered = text.casefold()
    for needle in _AUTHORITY_CLAIM_NEEDLES:
        if needle in lowered:
            raise CompanySkillError(
                "executor skill must not claim Captain review or merge authority"
            )
    mapping = _parse_frontmatter_mapping(head)
    if "name" not in mapping or "description" not in mapping:
        raise CompanySkillError("SKILL.md frontmatter requires name and description")
    fm_name = _scalar_yaml_text(mapping.get("name"))
    description = _scalar_yaml_text(mapping.get("description"))
    if not fm_name:
        raise CompanySkillError("SKILL.md frontmatter requires name and description")
    validate_skill_name(fm_name)
    return fm_name, description


def _assert_path_readable(path: Path) -> None:
    """Ensure a non-directory path can be opened for read before mutation."""

    try:
        if path.is_symlink():
            # Symlink to file: open the path (follows) to prove readability.
            if not path.exists():
                raise CompanySkillError("skill tree contains a broken symlink")
            if path.is_dir():
                return
        elif path.is_dir():
            return
        with path.open("rb") as handle:
            handle.read(1)
    except CompanySkillError:
        raise
    except OSError as exc:
        raise CompanySkillError("skill tree contains an unreadable path") from exc


def _assert_skill_tree_confined(skill_dir: Path, pack_root: Path) -> None:
    """Walk the skill tree, following safe links, rejecting escapes and dir links.

    Directory symlinks are rejected: ``shutil.copytree(..., symlinks=False)``
    materializes them and would otherwise copy nested escaping file targets that
    a non-following ``rglob`` never visited. File symlinks must resolve inside
    the pack. Cycles through repeated real directories are skipped.
    """

    try:
        resolved_pack = pack_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CompanySkillError("company pack source is not resolvable") from exc

    stack: list[Path] = [skill_dir]
    seen_real_dirs: set[Path] = set()
    while stack:
        current = stack.pop()
        try:
            if current.is_symlink():
                try:
                    target = current.resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    raise CompanySkillError(
                        "skill tree contains an unreadable path"
                    ) from exc
                if not _is_within(target, resolved_pack):
                    raise CompanySkillError(
                        "skill tree contains an escaping symlink"
                    )
                # Directory symlinks are rejected (copytree would materialize them).
                try:
                    is_dir_link = current.exists() and current.is_dir()
                except OSError as exc:
                    raise CompanySkillError(
                        "skill tree contains an unreadable path"
                    ) from exc
                if is_dir_link:
                    raise CompanySkillError(
                        "skill tree must not contain directory symlinks"
                    )
                _assert_path_readable(current)
                continue

            try:
                real = current.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise CompanySkillError(
                    "skill tree contains an unreadable path"
                ) from exc
            if not _is_within(real, resolved_pack):
                raise CompanySkillError("skill tree escapes the company pack root")

            if current.is_dir():
                if real in seen_real_dirs:
                    continue
                seen_real_dirs.add(real)
                try:
                    children = list(current.iterdir())
                except OSError as exc:
                    raise CompanySkillError(
                        "skill tree contains an unreadable path"
                    ) from exc
                stack.extend(children)
            elif current.is_file():
                _assert_path_readable(current)
            else:
                # sockets/devices etc.
                raise CompanySkillError("skill tree contains an unsupported path type")
        except CompanySkillError:
            raise
        except OSError as exc:
            raise CompanySkillError("skill tree contains an unreadable path") from exc


def _validate_skill_tree(skill_dir: Path, pack_root: Path) -> SkillCatalogEntry:
    if not skill_dir.is_dir():
        raise CompanySkillError("skill path is not a directory")
    dir_name = validate_skill_name(skill_dir.name)
    try:
        resolved_skill = skill_dir.resolve(strict=True)
        resolved_pack = pack_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CompanySkillError("skill path is not resolvable inside the pack") from exc
    if not _is_within(resolved_skill, resolved_pack):
        raise CompanySkillError("skill path escapes the company pack root")
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_symlink():
        try:
            md_resolved = skill_md.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CompanySkillError("SKILL.md symlink is not resolvable") from exc
        if not _is_within(md_resolved, resolved_pack):
            raise CompanySkillError("SKILL.md symlink escapes the company pack root")
    if not skill_md.is_file():
        raise CompanySkillError("skill missing SKILL.md entrypoint")
    if skill_md.stat().st_size < 1:
        raise CompanySkillError("skill SKILL.md is empty")
    _assert_skill_tree_confined(skill_dir, pack_root)
    fm_name, description = _parse_frontmatter_name_and_description(skill_md)
    if fm_name.casefold() != dir_name.casefold():
        raise CompanySkillError(
            "SKILL.md frontmatter name must match the skill directory name"
        )
    rel = str(Path(SKILLS_DIR_NAME) / dir_name)
    return SkillCatalogEntry(
        name=dir_name,
        role=ROLE_EXECUTOR,
        relative_path=rel,
        description=description,
        source="company_pack",
    )


def discover_pack_skills(source: Path) -> list[Path]:
    if not source.is_dir():
        raise CompanySkillError("company pack source is missing or not a directory")
    try:
        source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CompanySkillError("company pack source is not resolvable") from exc
    skills: list[Path] = []
    try:
        children = sorted(source.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise CompanySkillError("company pack source is unreadable") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        if child.is_dir():
            skills.append(child)
    return skills


def validate_company_pack(
    source: Path | None,
    allowlist: Sequence[str],
    *,
    require_selection: bool = True,
) -> ValidationResult:
    """Validate allowlisted skills under source without mutating state."""

    if source is None:
        return ValidationResult(
            ok=True,
            status=STATUS_SKIPPED,
            message="company skill pack is not configured",
        )
    try:
        pack = source.expanduser()
        if not pack.is_absolute():
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack source must be an absolute path",
                errors=("company pack source must be an absolute path",),
            )
        if ".." in pack.parts:
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack source contains an unsafe path element",
                errors=("company pack source contains an unsafe path element",),
            )
        if not pack.exists():
            return ValidationResult(
                ok=True,
                status=STATUS_PRESERVED,
                message="company pack source is missing; preserving last known-good import",
            )
        if not pack.is_dir():
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack source is not a directory",
                errors=("company pack source is not a directory",),
            )
        try:
            entries = list(pack.iterdir())
        except OSError:
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack source is unreadable",
                errors=("company pack source is unreadable",),
            )
        if not any(not child.name.startswith(".") for child in entries):
            return ValidationResult(
                ok=True,
                status=STATUS_PRESERVED,
                message="company pack source is empty; preserving last known-good import",
            )

        allow = []
        errors: list[str] = []
        seen: set[str] = set()
        for raw_name in allowlist:
            try:
                name = validate_skill_name(str(raw_name))
            except CompanySkillError as exc:
                errors.append(str(exc))
                continue
            key = name.casefold()
            if key in seen:
                errors.append("allowlist entries must be unique")
                continue
            seen.add(key)
            allow.append(name)
        if errors:
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack allowlist is invalid",
                errors=tuple(errors),
            )
        if not allow:
            return ValidationResult(
                ok=True,
                status=STATUS_PRESERVED,
                message="company pack allowlist is empty; preserving last known-good import",
            )

        allow_set = {name.casefold(): name for name in allow}
        selected: list[SkillCatalogEntry] = []
        found: set[str] = set()
        for skill_dir in discover_pack_skills(pack):
            key = skill_dir.name.casefold()
            if key not in allow_set:
                continue
            try:
                entry = _validate_skill_tree(skill_dir, pack)
            except CompanySkillError as exc:
                errors.append(f"{skill_dir.name}: {exc}")
                continue
            if entry.name.casefold() != allow_set[key].casefold():
                errors.append(f"{skill_dir.name}: directory name mismatch")
                continue
            selected.append(entry)
            found.add(key)
        missing = [allow_set[k] for k in allow_set if k not in found]
        for name in missing:
            errors.append(f"{name}: allowlisted skill not found in pack")
        if errors:
            return ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message="company pack validation failed",
                errors=tuple(errors),
                selected=tuple(selected),
            )
        if require_selection and not selected:
            return ValidationResult(
                ok=True,
                status=STATUS_PRESERVED,
                message="no allowlisted skills selected; preserving last known-good import",
            )
        selected_sorted = tuple(sorted(selected, key=lambda item: item.name))
        return ValidationResult(
            ok=True,
            status=STATUS_IMPORTED,
            message=f"validated {len(selected_sorted)} allowlisted skill(s)",
            selected=selected_sorted,
        )
    except CompanySkillError as exc:
        return ValidationResult(
            ok=False,
            status=STATUS_REJECTED,
            message=str(exc),
            errors=(str(exc),),
        )


def _active_link(state_root: Path) -> Path:
    return state_root / ACTIVE_NAME


def _releases_dir(state_root: Path) -> Path:
    return state_root / RELEASES_NAME


def _staging_dir(state_root: Path) -> Path:
    return state_root / STAGING_NAME


def executor_discovery_dir(state_root: Path) -> Path:
    """Path Hermes should scan for imported executor skills (active release)."""

    return state_root / ACTIVE_NAME / SKILLS_DIR_NAME


def read_active_manifest(state_root: Path) -> dict[str, object] | None:
    active = _active_link(state_root)
    if not active.exists():
        return None
    try:
        target = active.resolve(strict=True) if active.is_symlink() else active
        manifest_path = target / MANIFEST_NAME
        if not manifest_path.is_file():
            return None
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _active_release_id(state_root: Path) -> str | None:
    manifest = read_active_manifest(state_root)
    if not manifest:
        return None
    release_id = manifest.get("release_id")
    return release_id if isinstance(release_id, str) and release_id else None


def _catalog_generated_at(state_root: Path) -> str:
    """Stable catalog timestamp: active release imported_at, else empty string."""

    manifest = read_active_manifest(state_root)
    if not manifest:
        return ""
    imported = manifest.get("imported_at")
    if isinstance(imported, str) and imported:
        return imported
    release_id = manifest.get("release_id")
    if isinstance(release_id, str) and release_id:
        return release_id
    return ""


def list_installed_skills(state_root: Path) -> tuple[SkillCatalogEntry, ...]:
    manifest = read_active_manifest(state_root)
    if not manifest:
        return ()
    raw_skills = manifest.get("skills", [])
    if not isinstance(raw_skills, list):
        return ()
    entries: list[SkillCatalogEntry] = []
    for item in raw_skills:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        entries.append(
            SkillCatalogEntry(
                name=name,
                role=str(item.get("role") or ROLE_EXECUTOR),
                relative_path=str(item.get("relative_path") or f"{SKILLS_DIR_NAME}/{name}"),
                description=str(item.get("description") or ""),
                source=str(item.get("source") or "company_pack"),
            )
        )
    return tuple(sorted(entries, key=lambda item: item.name))


def available_captain_bundled_names() -> tuple[str, ...]:
    """Captain catalog entries derived from real package assets only."""

    try:
        from hermes_helmet.install_skills import iter_bundled_skills

        names = [path.name for path in iter_bundled_skills()]
        return tuple(sorted(names))
    except Exception:  # noqa: BLE001 - catalog must stay available without install deps
        bundled = Path(__file__).resolve().parent / "bundled_skills"
        if not bundled.is_dir():
            return ()
        found = [
            path.name
            for path in bundled.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        ]
        return tuple(sorted(found))


def default_hermes_config_path(*, hermes_home: Path | None = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home) / "config.yaml"
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env) / "config.yaml"
    return Path.home() / ".hermes" / "config.yaml"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _try_load_mapping(text: str) -> dict[str, object] | None:
    """Load a YAML mapping when PyYAML is available; None if unusable."""

    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        loaded = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _dump_mapping(data: dict[str, object]) -> str:
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    # Minimal emitter for skills.external_dirs-only documents.
    skills = data.get("skills")
    if not isinstance(skills, dict):
        return ""
    dirs = skills.get("external_dirs") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    lines = ["skills:", "  external_dirs:"]
    for item in dirs:
        lines.append(f"    - {_yaml_block_list_item_scalar(str(item))}")
    return "\n".join(lines) + "\n"


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _top_level_key_header_re(key: str) -> re.Pattern[str]:
    """Match plain or single-layer-quoted top-level ``key:`` headers.

    Prefer :func:`_top_level_block_span`, which normalizes escaped quoted keys.
    This pattern remains for simple plain/quoted spelling checks only.
    """

    escaped = re.escape(key)
    return re.compile(
        rf"^(?:{escaped}|\"{escaped}\"|'{escaped}')\s*:\s*(.*)$"
    )


def _normalize_config_top_level_key(raw_key: str) -> str:
    """Normalize a top-level config key the way a YAML loader would."""

    try:
        return _normalize_yaml_mapping_key(raw_key)
    except CompanySkillError as exc:
        raise CompanySkillError(
            "hermes config top-level key uses unsupported syntax and cannot be "
            "updated safely without a structured YAML parser"
        ) from exc


def _top_level_block_span(lines: list[str], key: str) -> tuple[int, int] | None:
    """Return [start, end) line indices for a top-level ``key:`` mapping block.

    Keys are normalized through the same plain/quoted/escape rules a YAML loader
    uses, so ``\"skills\":`` and ``\"ski\\u006cls\":`` both identify the skills map.
    Multiple effective headers for the same key are rejected by callers before
    mutation.
    """

    starts: list[int] = []
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _line_indent(line) != 0:
            continue
        stripped = line.rstrip("\n")
        if ":" not in stripped:
            continue
        raw_key, _, _rest = stripped.partition(":")
        normalized = _normalize_config_top_level_key(raw_key)
        if normalized == key:
            starts.append(index)
    if not starts:
        return None
    if len(starts) > 1:
        raise CompanySkillError(
            f"hermes config has duplicate top-level '{key}' keys and cannot be "
            "updated safely without a structured YAML parser"
        )
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _line_indent(line) == 0:
            end = index
            break
    return start, end


def _top_level_mentions_key(lines: list[str], key: str) -> bool:
    """True when a top-level line names ``key`` in a form the rewriter skips.

    Used only after the supported header match failed, so a second ``skills``
    block is not appended on top of an already-effective skills map. Any
    unparseable top-level key is treated as unsafe.
    """

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _line_indent(line) != 0:
            continue
        stripped = line.rstrip("\n")
        if ":" not in stripped:
            continue
        raw_key, _, _rest = stripped.partition(":")
        try:
            normalized = _normalize_yaml_mapping_key(raw_key)
        except CompanySkillError:
            return True
        if normalized == key:
            return True
        # Residual near-misses: optional spaces inside quotes, or mixed pairs.
        residual = re.compile(
            rf"""^(?:
                ["']\s*{re.escape(key)}\s*["']
                |["']{re.escape(key)}
                |{re.escape(key)}["']
            )\s*:""",
            re.VERBOSE,
        )
        if residual.match(raw_key.strip() + ":"):
            return True
    return False


def _external_dirs_registered(text: str, discovery: str) -> bool:
    """Whether config text actually places discovery under skills.external_dirs."""

    data = _try_load_mapping(text)
    if data is not None:
        skills = data.get("skills")
        if not isinstance(skills, dict):
            return False
        raw = skills.get("external_dirs") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return False
        wanted = {str(discovery), str(Path(discovery))}
        try:
            wanted.add(str(Path(discovery).resolve(strict=False)))
        except (OSError, RuntimeError):
            pass
        for item in raw:
            text_item = str(item).strip()
            if not text_item:
                continue
            if text_item in wanted:
                return True
            try:
                if str(Path(text_item).resolve(strict=False)) in wanted:
                    return True
            except (OSError, RuntimeError):
                continue
        return False

    # Structured-less verification: discovery must appear as a list item inside
    # the top-level skills block's external_dirs subsection, not under a later key.
    # Compare path identity via resolve so macOS /var vs /private/var forms match.
    wanted = _path_identity_set(discovery)
    lines = text.splitlines()
    try:
        span = _top_level_block_span(lines, "skills")
    except CompanySkillError:
        return False
    if span is None:
        return False
    start, end = span
    block = lines[start:end]
    ext_at = None
    ext_indent = None
    for index, line in enumerate(block):
        match = re.match(r"^(\s*)external_dirs\s*:\s*(.*)$", line)
        if match is None:
            continue
        ext_at = index
        ext_indent = len(match.group(1))
        inline = match.group(2).strip()
        if _path_text_matches_wanted(inline, wanted):
            return True
        break
    if ext_at is None or ext_indent is None:
        return False
    for line in block[ext_at + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = _line_indent(line)
        if indent <= ext_indent:
            break
        stripped = line.strip()
        if stripped.startswith("-"):
            item = stripped[1:].strip().strip("\"'")
            if item in wanted or item.rstrip("/") in {
                w.rstrip("/") for w in wanted
            }:
                return True
            if _path_text_matches_wanted(item, wanted):
                return True
    return False


def _path_identity_set(path_text: str) -> set[str]:
    """Return string forms that identify the same filesystem path."""

    wanted = {str(path_text), str(Path(path_text))}
    try:
        wanted.add(str(Path(path_text).resolve(strict=False)))
    except (OSError, RuntimeError):
        pass
    return wanted


def _path_text_matches_wanted(candidate: str, wanted: set[str]) -> bool:
    """True when candidate names any path in wanted (raw or resolved)."""

    text = candidate.strip().strip("\"'")
    if not text:
        return False
    if text in wanted or text.rstrip("/") in {w.rstrip("/") for w in wanted}:
        return True
    try:
        resolved = str(Path(text).resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    return resolved in wanted or resolved.rstrip("/") in {
        w.rstrip("/") for w in wanted
    }


def _ensure_external_dir_text(text: str, discovery: str) -> str:
    """Merge discovery into skills.external_dirs without a YAML library.

    Inserts inside the top-level ``skills`` block so later siblings such as
    ``terminal`` cannot absorb the new key. Accepts plain or quoted ``skills``
    headers. Raises when the existing document cannot be updated safely (inline
    flow collections, tabs, unreadable shape, unsupported key syntax).
    """

    if not text.strip():
        return _dump_mapping({"skills": {"external_dirs": [discovery]}})
    if _external_dirs_registered(text, discovery):
        return text if text.endswith("\n") else text + "\n"
    if "\t" in text:
        raise CompanySkillError(
            "hermes config uses tab indentation and cannot be updated safely "
            "without a structured YAML parser"
        )

    lines = text.splitlines()
    span = _top_level_block_span(lines, "skills")
    if span is None:
        if _top_level_mentions_key(lines, "skills"):
            raise CompanySkillError(
                "hermes config skills key uses unsupported syntax and cannot be "
                "updated safely without a structured YAML parser"
            )
        body = text.rstrip("\n")
        addition = (
            "\n\nskills:\n  external_dirs:\n"
            f"    - {_yaml_block_list_item_scalar(discovery)}\n"
        )
        return body + addition

    start, end = span
    header = lines[start]
    _, _, header_rest_raw = header.rstrip("\n").partition(":")
    header_rest = header_rest_raw.strip()
    if header_rest and header_rest not in ("|", ">", ">-", "|-"):
        raise CompanySkillError(
            "hermes config skills value is not a block mapping and cannot be "
            "updated safely without a structured YAML parser"
        )

    block = lines[start:end]
    ext_at = None
    ext_indent = 2
    for index, line in enumerate(block):
        match = re.match(r"^(\s*)external_dirs\s*:\s*(.*)$", line)
        if match is None:
            continue
        # Only accept external_dirs nested under skills (indent > 0).
        indent = len(match.group(1))
        if indent == 0:
            continue
        ext_at = index
        ext_indent = indent
        inline = match.group(2).strip()
        if inline.startswith("["):
            raise CompanySkillError(
                "hermes config skills.external_dirs is an inline list and cannot "
                "be updated safely without a structured YAML parser"
            )
        if inline and inline not in ("|", ">", ">-", "|-"):
            # Inline scalar: rewrite as a block list so the existing path is
            # preserved and discovery can be appended with exact identity.
            try:
                existing_item = _unquote_yaml_scalar(inline)
            except CompanySkillError as exc:
                raise CompanySkillError(
                    "hermes config skills.external_dirs uses unsupported scalar "
                    "syntax and cannot be updated safely without a structured "
                    "YAML parser"
                ) from exc
            if not existing_item:
                raise CompanySkillError(
                    "hermes config skills.external_dirs uses unsupported scalar "
                    "syntax and cannot be updated safely without a structured "
                    "YAML parser"
                )
            lines[start + index] = f"{' ' * indent}external_dirs:"
            encoded_existing = _yaml_block_list_item_scalar(existing_item)
            encoded_discovery = _yaml_block_list_item_scalar(discovery)
            lines.insert(start + index + 1, f"{' ' * (indent + 2)}- {encoded_existing}")
            lines.insert(start + index + 2, f"{' ' * (indent + 2)}- {encoded_discovery}")
            return "\n".join(lines).rstrip("\n") + "\n"
        break

    item_line = f"{' ' * (ext_indent + 2)}- {_yaml_block_list_item_scalar(discovery)}"
    if ext_at is None:
        insert_at = start + len(block)
        # Keep blank lines at end of block with the following section.
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{' ' * ext_indent}external_dirs:")
        lines.insert(insert_at + 1, item_line)
    else:
        insert_at = start + ext_at + 1
        while insert_at < end and (
            not lines[insert_at].strip() or _line_indent(lines[insert_at]) > ext_indent
        ):
            if lines[insert_at].strip() and _line_indent(lines[insert_at]) <= ext_indent:
                break
            insert_at += 1
        # Walk only children of external_dirs.
        cursor = start + ext_at + 1
        while cursor < end:
            line = lines[cursor]
            if not line.strip() or line.lstrip().startswith("#"):
                cursor += 1
                continue
            if _line_indent(line) <= ext_indent:
                break
            cursor += 1
        lines.insert(cursor, item_line)

    return "\n".join(lines).rstrip("\n") + "\n"


def _merge_external_dir_config(existing: str, discovery: str) -> str:
    """Return updated config text with discovery registered under skills.external_dirs."""

    data = _try_load_mapping(existing)
    if data is not None:
        skills_obj = data.get("skills")
        if not isinstance(skills_obj, dict):
            skills_obj = {}
            data["skills"] = skills_obj
        raw_dirs = skills_obj.get("external_dirs") or []
        if isinstance(raw_dirs, str):
            raw_dirs = [raw_dirs]
        if not isinstance(raw_dirs, list):
            raw_dirs = []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_dirs:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        if discovery not in seen:
            normalized.append(discovery)
        skills_obj["external_dirs"] = normalized
        return _dump_mapping(data)
    return _ensure_external_dir_text(existing, discovery)


def register_executor_discovery(
    state_root: Path,
    *,
    hermes_home: Path | None = None,
    config_path: Path | None = None,
) -> Path | None:
    """Register the active executor skills dir in Hermes ``skills.external_dirs``.

    Never writes skill bytes into host-global Codex/Claude/Hermes skill trees.
    Registers the stable ``…/active/skills`` path so release rotation stays
    discoverable through the active symlink. Returns that discovery directory
    only when registration is verified in config; None when there is no active
    skills dir. Raises ``CompanySkillError`` when registration cannot be
    performed or verified (caller must not claim a registered path).
    """

    root = assert_not_host_global_skill_path(state_root)
    discovery = executor_discovery_dir(root)
    if not discovery.is_dir():
        return None
    try:
        discovery_resolved = discovery.resolve(strict=True)
    except (OSError, RuntimeError):
        discovery_resolved = discovery
    assert_not_host_global_skill_path(discovery_resolved, field="discovery_path")
    # Stable path survives release rotation via the active symlink.
    stable = str(discovery)

    cfg_path = (
        Path(config_path)
        if config_path is not None
        else default_hermes_config_path(hermes_home=hermes_home)
    )
    assert_not_host_global_skill_path(cfg_path.parent, field="hermes_config_parent")

    existing = ""
    if cfg_path.is_file():
        try:
            existing = cfg_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CompanySkillError("hermes config is unreadable") from exc

    new_text = _merge_external_dir_config(existing, stable)
    if not new_text.endswith("\n"):
        new_text += "\n"
    if not _external_dirs_registered(new_text, stable):
        raise CompanySkillError(
            "hermes skills.external_dirs registration could not be verified"
        )
    _atomic_write_text(cfg_path, new_text)

    try:
        written = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompanySkillError("hermes config is unreadable after registration") from exc
    if not _external_dirs_registered(written, stable):
        raise CompanySkillError(
            "hermes skills.external_dirs registration could not be verified"
        )
    return discovery


def build_skill_catalog(
    *,
    policy: Policy | None = None,
    state_root: Path | None = None,
    captain_bundled: Sequence[str] | None = None,
) -> SkillCatalog:
    """Build a deterministic catalog of Captain vs executor skill boundaries."""

    root = assert_not_host_global_skill_path(
        state_root if state_root is not None else default_state_root()
    )
    cfg = policy.skills if policy is not None else CompanySkillsConfig()
    installed = list_installed_skills(root)
    active_manifest = read_active_manifest(root)
    active_release = None
    if active_manifest and isinstance(active_manifest.get("release_id"), str):
        active_release = active_manifest["release_id"]

    if captain_bundled is not None:
        captain_names = list(captain_bundled)
    else:
        captain_names = list(available_captain_bundled_names())
    captain_entries = tuple(
        SkillCatalogEntry(
            name=name,
            role=ROLE_CAPTAIN,
            relative_path=f"bundled_skills/{name}",
            description=(
                "Captain/orchestrator skill; install only via explicit "
                "setup-helmet/install-skills"
            ),
            source="bundled",
        )
        for name in captain_names
    )

    combined = captain_entries + installed
    source = str(cfg.source) if cfg.source is not None else None
    discovery = executor_discovery_dir(root)
    discovery_text = str(discovery) if discovery.is_dir() else None
    if installed:
        status = STATUS_IMPORTED
        message = f"{len(installed)} executor skill(s) active in Hermes-owned state"
    elif not cfg.configured:
        status = STATUS_SKIPPED
        message = "company skill pack is not configured"
    else:
        status = STATUS_SKIPPED
        message = "no active company skill import"

    return SkillCatalog(
        status=status,
        role_boundary="captain_setup_vs_executor_import",
        skills=combined,
        allowlist=cfg.allowlist,
        source=source,
        state_root=str(root),
        active_release=active_release,
        message=message,
        generated_at=_catalog_generated_at(root),
        discovery_path=discovery_text,
    )


def _write_manifest(
    release_dir: Path,
    *,
    release_id: str,
    source: Path,
    allowlist: Sequence[str],
    entries: Sequence[SkillCatalogEntry],
) -> None:
    payload = {
        "version": 1,
        "release_id": release_id,
        "role_boundary": ROLE_EXECUTOR,
        "source": str(source),
        "allowlist": list(allowlist),
        "skills": [entry.to_public_dict() for entry in entries],
        "imported_at": _utc_now(),
    }
    path = release_dir / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o644)


def _copy_skill(skill_src: Path, dest_parent: Path) -> None:
    dest = dest_parent / skill_src.name
    # Do not follow symlinks: directory symlinks are rejected at validation time;
    # file symlinks that stayed in-pack are copied as links then we refuse links
    # by using symlinks=False only after confinement checks.
    shutil.copytree(skill_src, dest, symlinks=False, dirs_exist_ok=False)
    for path in [dest, *dest.rglob("*")]:
        if path.is_dir():
            os.chmod(path, 0o755)
        elif path.is_file():
            os.chmod(path, 0o644)


def _atomic_activate(state_root: Path, release_dir: Path) -> None:
    state_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    active = _active_link(state_root)
    # Prefer relative symlink for portability inside the state root.
    rel_target = Path(RELEASES_NAME) / release_dir.name
    tmp_link = state_root / f".active-tmp-{release_dir.name}"
    if tmp_link.exists() or tmp_link.is_symlink():
        if tmp_link.is_dir() and not tmp_link.is_symlink():
            shutil.rmtree(tmp_link)
        else:
            tmp_link.unlink()
    tmp_link.symlink_to(rel_target, target_is_directory=True)
    os.replace(tmp_link, active)


def _assert_owned_state_path(root: Path, path: Path, *, field: str) -> Path:
    """Require path (and its resolve) to stay inside Hermes-owned state root."""

    assert_not_host_global_skill_path(path, field=field)
    try:
        root_resolved = root.resolve(strict=False)
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CompanySkillError(f"{field}: is not a usable filesystem path") from exc
    assert_not_host_global_skill_path(resolved, field=field)
    if resolved != root_resolved and not _is_within(resolved, root_resolved):
        raise CompanySkillError(
            f"{field}: must remain inside Hermes-owned company skill state"
        )
    # Symlink children must not leave the owned root even before final resolve of
    # nested targets is considered — the link path itself is under root, but its
    # ultimate target is checked above via resolve().
    if path.is_symlink():
        try:
            # Also reject when the immediate link target (may be relative) escapes
            # after join+resolve against the parent.
            link_parent = path.parent.resolve(strict=False)
            immediate = (link_parent / path.readlink()).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise CompanySkillError(f"{field}: is not a usable filesystem path") from exc
        if immediate != root_resolved and not _is_within(immediate, root_resolved):
            raise CompanySkillError(
                f"{field}: must remain inside Hermes-owned company skill state"
            )
        assert_not_host_global_skill_path(immediate, field=field)
    return resolved


def _constrain_state_layout(root: Path) -> None:
    """Reject state children that leave Hermes-owned persistent state."""

    assert_not_host_global_skill_path(root, field="state_root")
    for rel, field in (
        (RELEASES_NAME, "releases"),
        (STAGING_NAME, "staging"),
        (ACTIVE_NAME, "active"),
    ):
        child = root / rel
        if child.exists() or child.is_symlink():
            _assert_owned_state_path(root, child, field=field)


def import_company_skills(
    *,
    source: Path | None,
    allowlist: Sequence[str],
    state_root: Path | None = None,
    dry_run: bool = False,
    hermes_home: Path | None = None,
    hermes_config_path: Path | None = None,
    register_discovery: bool = True,
) -> ImportResult:
    """Validate then atomically import allowlisted skills into Hermes-owned state.

    Never mutates host-global Codex/Claude/Hermes skill directories. On validation
    failure or missing/empty sources, leaves any last known-good import intact.
    """

    root = assert_not_host_global_skill_path(
        state_root if state_root is not None else default_state_root()
    )
    existing = list_installed_skills(root)
    existing_names = tuple(entry.name for entry in existing)
    allow_tuple = tuple(str(item) for item in allowlist)
    existing_discovery = (
        str(executor_discovery_dir(root))
        if executor_discovery_dir(root).is_dir()
        else None
    )

    def _catalog(
        *,
        status: str,
        message: str,
        skills: tuple[SkillCatalogEntry, ...] | None = None,
        release_id: str | None = None,
        source_text: str | None = None,
        discovery_path: str | None = None,
    ) -> SkillCatalog:
        return SkillCatalog(
            status=status,
            role_boundary="captain_setup_vs_executor_import",
            skills=existing if skills is None else skills,
            allowlist=allow_tuple,
            source=source_text if source_text is not None else (str(source) if source else None),
            state_root=str(root),
            active_release=release_id if release_id is not None else _active_release_id(root),
            message=message,
            generated_at=_catalog_generated_at(root),
            discovery_path=discovery_path
            if discovery_path is not None
            else existing_discovery,
        )

    if source is None:
        validation = ValidationResult(
            ok=True,
            status=STATUS_SKIPPED,
            message="company skill pack is not configured",
        )
        return ImportResult(
            status=STATUS_SKIPPED,
            message=validation.message,
            catalog=_catalog(status=STATUS_SKIPPED, message=validation.message, source_text=None),
            validation=validation,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )

    validation = validate_company_pack(source, allowlist)
    if validation.status == STATUS_SKIPPED:
        return ImportResult(
            status=STATUS_SKIPPED,
            message=validation.message,
            catalog=_catalog(status=STATUS_SKIPPED, message=validation.message),
            validation=validation,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )
    if validation.status == STATUS_PRESERVED:
        status = STATUS_PRESERVED if existing else STATUS_SKIPPED
        return ImportResult(
            status=status,
            message=validation.message,
            catalog=_catalog(status=status, message=validation.message),
            validation=validation,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )
    if not validation.ok or validation.status == STATUS_REJECTED:
        return ImportResult(
            status=STATUS_REJECTED,
            message=validation.message,
            catalog=_catalog(status=STATUS_REJECTED, message=validation.message),
            validation=validation,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )

    selected = validation.selected
    if dry_run:
        message = f"dry-run: would import {len(selected)} skill(s)"
        return ImportResult(
            status=STATUS_IMPORTED,
            message=message,
            catalog=_catalog(status=STATUS_IMPORTED, message=message, skills=selected),
            validation=validation,
            installed_names=tuple(entry.name for entry in selected),
            mutated=False,
            discovery_path=existing_discovery,
        )

    try:
        _constrain_state_layout(root)
    except CompanySkillError as exc:
        rejected = ValidationResult(
            ok=False,
            status=STATUS_REJECTED,
            message=str(exc),
            errors=(str(exc),),
            selected=selected,
        )
        return ImportResult(
            status=STATUS_REJECTED,
            message=str(exc),
            catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
            validation=rejected,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )

    release_id = _release_id()
    releases = _releases_dir(root)
    # Constrain releases path before mkdir and after (symlink indirection).
    if releases.exists() or releases.is_symlink():
        try:
            _assert_owned_state_path(root, releases, field="releases")
        except CompanySkillError as exc:
            rejected = ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message=str(exc),
                errors=(str(exc),),
                selected=selected,
            )
            return ImportResult(
                status=STATUS_REJECTED,
                message=str(exc),
                catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
                validation=rejected,
                installed_names=existing_names,
                mutated=False,
                discovery_path=existing_discovery,
            )
    try:
        releases.mkdir(parents=True, exist_ok=True, mode=0o755)
        _assert_owned_state_path(root, releases, field="releases")
    except CompanySkillError as exc:
        rejected = ValidationResult(
            ok=False,
            status=STATUS_REJECTED,
            message=str(exc),
            errors=(str(exc),),
            selected=selected,
        )
        return ImportResult(
            status=STATUS_REJECTED,
            message=str(exc),
            catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
            validation=rejected,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )
    except OSError as exc:
        raise CompanySkillError("company skill import failed before activation") from exc

    previous_active = None
    active = _active_link(root)
    if active.exists() or active.is_symlink():
        try:
            _assert_owned_state_path(root, active, field="active")
            previous_active = active.resolve(strict=True)
        except CompanySkillError as exc:
            rejected = ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message=str(exc),
                errors=(str(exc),),
                selected=selected,
            )
            return ImportResult(
                status=STATUS_REJECTED,
                message=str(exc),
                catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
                validation=rejected,
                installed_names=existing_names,
                mutated=False,
                discovery_path=existing_discovery,
            )
        except (OSError, RuntimeError):
            previous_active = None

    staging_parent = _staging_dir(root)
    if staging_parent.exists() or staging_parent.is_symlink():
        try:
            _assert_owned_state_path(root, staging_parent, field="staging")
        except CompanySkillError as exc:
            rejected = ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message=str(exc),
                errors=(str(exc),),
                selected=selected,
            )
            return ImportResult(
                status=STATUS_REJECTED,
                message=str(exc),
                catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
                validation=rejected,
                installed_names=existing_names,
                mutated=False,
                discovery_path=existing_discovery,
            )
    try:
        staging_parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        _assert_owned_state_path(root, staging_parent, field="staging")
    except CompanySkillError as exc:
        rejected = ValidationResult(
            ok=False,
            status=STATUS_REJECTED,
            message=str(exc),
            errors=(str(exc),),
            selected=selected,
        )
        return ImportResult(
            status=STATUS_REJECTED,
            message=str(exc),
            catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
            validation=rejected,
            installed_names=existing_names,
            mutated=False,
            discovery_path=existing_discovery,
        )
    except OSError as exc:
        raise CompanySkillError("company skill import failed before activation") from exc

    staging = Path(tempfile.mkdtemp(prefix=f"rel-{release_id}-", dir=str(staging_parent)))
    try:
        _assert_owned_state_path(root, staging, field="staging")
    except CompanySkillError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    skills_dest = staging / SKILLS_DIR_NAME
    skills_dest.mkdir(parents=True, mode=0o755)
    final_dir: Path | None = None
    try:
        pack = source.expanduser().resolve(strict=True)
        for entry in selected:
            skill_src = pack / entry.name
            _validate_skill_tree(skill_src, pack)
            _copy_skill(skill_src, skills_dest)
        _write_manifest(
            staging,
            release_id=release_id,
            source=pack,
            allowlist=allowlist,
            entries=selected,
        )
        final_dir = releases / release_id
        if final_dir.exists() or final_dir.is_symlink():
            raise CompanySkillError("release id collision")
        _assert_owned_state_path(root, final_dir.parent, field="releases")
        os.replace(staging, final_dir)
        _assert_owned_state_path(root, final_dir, field="release")
        _atomic_activate(root, final_dir)
        _assert_owned_state_path(root, _active_link(root), field="active")
    except Exception as exc:
        try:
            if previous_active is not None and previous_active.is_dir():
                try:
                    previous_active.relative_to(releases.resolve(strict=False))
                except ValueError:
                    pass
                else:
                    _atomic_activate(root, previous_active)
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass
        if staging.exists() and _is_within(staging, staging_parent):
            shutil.rmtree(staging, ignore_errors=True)
        if final_dir is not None and final_dir.exists() and not (
            active.exists() or active.is_symlink()
        ):
            shutil.rmtree(final_dir, ignore_errors=True)
        if isinstance(exc, CompanySkillError):
            # Preserve last-good: surface as rejected result rather than crash when
            # post-validation confinement/copy fails.
            rejected = ValidationResult(
                ok=False,
                status=STATUS_REJECTED,
                message=str(exc),
                errors=(str(exc),),
                selected=selected,
            )
            return ImportResult(
                status=STATUS_REJECTED,
                message=str(exc),
                catalog=_catalog(status=STATUS_REJECTED, message=str(exc)),
                validation=rejected,
                installed_names=existing_names,
                mutated=False,
                discovery_path=existing_discovery,
            )
        raise CompanySkillError(
            "company skill import failed before activation"
        ) from exc

    # Only claim a discovery path when Hermes config registration is verified.
    # Local skill bytes may still land under active/skills; directory existence
    # alone is not registration.
    discovery_path: str | None = None
    registration_note = ""
    if register_discovery:
        try:
            registered = register_executor_discovery(
                root,
                hermes_home=hermes_home,
                config_path=hermes_config_path,
            )
            if registered is not None:
                discovery_path = str(registered)
        except CompanySkillError as exc:
            # Import already activated; do not roll back last-good skill bytes.
            # Surface registration failure truthfully without claiming a path.
            registration_note = f"; discovery registration failed: {exc}"
    elif executor_discovery_dir(root).is_dir():
        # Caller opted out of config registration; still report local path for tests.
        discovery_path = str(executor_discovery_dir(root))

    installed = list_installed_skills(root)
    message = (
        f"imported {len(installed)} allowlisted skill(s) into Hermes-owned state"
        f"{registration_note}"
    )
    catalog = _catalog(
        status=STATUS_IMPORTED,
        message=message,
        skills=installed,
        release_id=release_id,
        discovery_path=discovery_path,
    )
    return ImportResult(
        status=STATUS_IMPORTED,
        message=message,
        catalog=catalog,
        validation=validation,
        installed_names=tuple(entry.name for entry in installed),
        release_id=release_id,
        mutated=True,
        discovery_path=discovery_path,
    )


def import_company_skills_from_policy(
    policy: Policy,
    *,
    state_root: Path | None = None,
    dry_run: bool = False,
    hermes_home: Path | None = None,
    hermes_config_path: Path | None = None,
    register_discovery: bool = True,
) -> ImportResult:
    """Run company pack import using the authority policy skills block."""

    cfg = policy.skills
    if not cfg.configured:
        return import_company_skills(
            source=None,
            allowlist=(),
            state_root=state_root,
            dry_run=dry_run,
            hermes_home=hermes_home,
            hermes_config_path=hermes_config_path,
            register_discovery=register_discovery,
        )
    return import_company_skills(
        source=cfg.source,
        allowlist=cfg.allowlist,
        state_root=state_root,
        dry_run=dry_run,
        hermes_home=hermes_home,
        hermes_config_path=hermes_config_path,
        register_discovery=register_discovery,
    )


def captain_install_boundary_message() -> str:
    return (
        "Captain/orchestrator bundled skills install only through explicit "
        "setup action (helmet install-skills). Executor company packs "
        "import only into Hermes-owned persistent state and cannot grant "
        "review or merge authority."
    )
