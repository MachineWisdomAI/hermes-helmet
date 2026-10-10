"""Read-only Claude Code session record reader for Captain's Bridge.

``helmet bridge read`` turns one Claude Code session's saved JSONL transcript
(plus that session's subagent transcripts) into a bounded summary of records
with stable references. Files are opened for reading only; nothing is written,
no Claude Code process is launched and no transcript is exported.

Claude Code's transcript format is private and version dependent. The
``SUPPORTED_VERSIONS`` range and the record-type tables below are the
compatibility boundary: anything outside them is reported as
``unsupported-format`` instead of an empty success.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from hermes_helmet import __version__

SCHEMA = "hermes-helmet.bridge.records/1"
DEFAULT_MAX_BYTES = 600000

# Claude Code versions whose transcripts this reader understands (inclusive
# lower bound, exclusive upper bound). Re-verify when the range changes.
SUPPORTED_VERSIONS = ((2, 0, 0), (2, 2, 0))

# Record types the reader understands and must be well formed.
CORE_TYPES = frozenset({"user", "assistant", "system"})
# Known metadata record types that carry no conversation content. They are
# skipped without being counted as unsupported.
KNOWN_NONCORE_TYPES = frozenset(
    {
        "summary",
        "file-history-snapshot",
        "queue-operation",
        "progress",
        "attachment",
        "last-prompt",
        "custom-title",
        "ai-title",
        "agent-name",
        "mode",
        "permission-mode",
        "pr-link",
        "tag",
        "content-replacement",
    }
)
# Content blocks that are model reasoning; dropped entirely.
REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking", "reasoning"})

ERROR_CODES = (
    "session-not-found",
    "ambiguous-session",
    "session-mismatch",
    "unsupported-format",
    "unreadable",
)

SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
UUID_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}")

MIN_REF_LENGTH = 8
# Per-text read-time caps so a single enormous tool result cannot exhaust
# memory. Person prompts and assistant text are never capped.
TOOL_RESULT_READ_CAP = 20000
TOOL_INPUT_CAP = 2000
# Head/tail excerpt applied to tool results when the budget is exceeded.
EXCERPT_HEAD = 600
EXCERPT_TAIL = 400

_HOOK_PREFIXES = ("<system-reminder>", "<user-prompt-submit-hook>")
_META_PREFIXES = ("<local-command-", "<command-name>", "<command-message>")


class BridgeReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "error": {"code": code, "message": message}}


@dataclass
class _Rec:
    uuid: str
    parent_uuid: str | None
    agent_id: str | None
    timestamp: str | None
    role: str
    origin: str
    text: str
    tool: str | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    truncated: bool = False
    orig_bytes: int = 0
    primary_uuid: str | None = None  # set on extra blocks of one record
    order: int = 0


@dataclass
class _State:
    records: list[_Rec] = field(default_factory=list)
    parent_of: dict[str, str | None] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)
    tool_use_uuid: dict[str, str] = field(default_factory=dict)
    agent_tool_use: dict[str, str] = field(default_factory=dict)
    unsupported: int = 0
    unsupported_types: set[str] = field(default_factory=set)
    pending_tail: bool = False
    last_main_uuid: str | None = None
    counter: int = 0


def claude_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Locating files
# --------------------------------------------------------------------------


def find_session_file(session_id: str, projects_dir: Path) -> Path:
    """Find the one transcript named exactly ``<session_id>.jsonl``."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise BridgeReadError(
            "session-not-found", "The session id is not a valid Claude Code session id."
        )
    matches: list[Path] = []
    try:
        with os.scandir(projects_dir) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                candidate = Path(entry.path) / f"{session_id}.jsonl"
                if candidate.is_file():
                    matches.append(candidate)
    except FileNotFoundError:
        raise BridgeReadError(
            "session-not-found",
            "No Claude Code projects directory was found, so the session "
            "transcript cannot be located. Pass --transcript with its path.",
        ) from None
    except OSError as exc:
        raise BridgeReadError(
            "unreadable", f"The Claude Code projects directory is not readable: {exc.strerror or exc}"
        ) from exc
    if not matches:
        raise BridgeReadError(
            "session-not-found",
            f"No transcript named {session_id}.jsonl was found under the Claude "
            "Code projects directory.",
        )
    if len(matches) > 1:
        raise BridgeReadError(
            "ambiguous-session",
            f"{len(matches)} transcripts named {session_id}.jsonl were found; "
            "pass --transcript to choose one explicitly.",
        )
    return matches[0]


def _subagent_files(transcript: Path, session_id: str) -> list[Path]:
    folder = transcript.parent / session_id / "subagents"
    try:
        with os.scandir(folder) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(".jsonl") and entry.is_file()
            )
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise BridgeReadError(
            "unreadable", f"The subagent transcripts are not readable: {exc.strerror or exc}"
        ) from exc
    return [folder / name for name in names]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _iter_lines(path: Path) -> Iterator[tuple[int, bytes, bool]]:
    """Yield (line number, raw line, terminated) streaming, read-only."""
    try:
        with open(path, "rb") as handle:
            number = 0
            for raw in handle:
                number += 1
                terminated = raw.endswith(b"\n")
                yield number, raw.rstrip(b"\r\n"), terminated
    except FileNotFoundError:
        raise BridgeReadError("session-not-found", f"Transcript {path.name} does not exist.") from None
    except OSError as exc:
        raise BridgeReadError(
            "unreadable", f"Transcript {path.name} is not readable: {exc.strerror or exc}"
        ) from exc


def _check_version(value: Any, where: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise BridgeReadError(
            "unsupported-format",
            f"{where} has no recognizable Claude Code version (found {value!r}).",
        )
    parts = tuple(int(part) for part in VERSION_RE.fullmatch(value).groups())  # type: ignore[union-attr]
    low, high = SUPPORTED_VERSIONS
    if not (low <= parts < high):
        raise BridgeReadError(
            "unsupported-format",
            f"Claude Code version {value} is outside the supported range "
            f"{'.'.join(map(str, low))} to below {'.'.join(map(str, high))}.",
        )
    return value


def _clip(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    head = cap * 3 // 5
    tail = cap - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} characters omitted]…\n{text[-tail:]}", True


def _blocks(content: Any, where: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append({"type": "text", "text": block})
            elif isinstance(block, dict):
                out.append(block)
            else:
                raise BridgeReadError("unsupported-format", f"{where} has a malformed content block.")
        return out
    raise BridgeReadError("unsupported-format", f"{where} has malformed message content.")


def _text_field(block: dict[str, Any], where: str, version: str) -> str:
    text = block.get("text")
    if not isinstance(text, str):
        raise BridgeReadError(
            "unsupported-format",
            f"{where} (Claude Code {version}) has a text block whose text is not a string.",
        )
    return text


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _user_origin(record: dict[str, Any], text: str, sidechain: bool) -> str:
    """Classify a user-role text record from recorded evidence only."""
    if sidechain:
        return "agent"
    marker = record.get("origin")
    kind = marker.get("kind") if isinstance(marker, dict) else marker
    if isinstance(kind, str):
        lowered = kind.lower()
        if lowered in {"human", "user", "person"}:
            return "person"
        if lowered == "hook":
            return "hook"
        if lowered == "plugin":
            return "plugin"
        return "meta"
    if record.get("isMeta") is True or record.get("isCompactSummary") is True:
        return "meta"
    if record.get("pluginName") or record.get("plugin"):
        return "plugin"
    stripped = text.lstrip()
    if stripped.startswith(_HOOK_PREFIXES):
        return "hook"
    if stripped.startswith(_META_PREFIXES):
        return "meta"
    return "person"


def _scan_file(
    path: Path,
    session_id: str,
    state: _State,
    *,
    agent_id: str | None,
) -> None:
    sidechain = agent_id is not None
    where_base = path.name
    for number, raw, terminated in _iter_lines(path):
        if not raw.strip():
            continue
        where = f"{where_base} line {number}"
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if not terminated:
                state.pending_tail = True
                continue
            raise BridgeReadError(
                "unsupported-format", f"{where} is not a valid JSON record."
            ) from None
        if not isinstance(record, dict):
            raise BridgeReadError("unsupported-format", f"{where} is not a JSON object record.")
        rtype = record.get("type")
        if not isinstance(rtype, str) or not rtype:
            raise BridgeReadError("unsupported-format", f"{where} has no record type.")
        sid = record.get("sessionId")
        if sid is not None and sid != session_id:
            raise BridgeReadError(
                "session-mismatch",
                f"{where} belongs to session {sid!r}, not the requested session {session_id!r}.",
            )
        if rtype not in CORE_TYPES:
            if record.get("version") is not None:
                _check_version(record["version"], where)
            if rtype not in KNOWN_NONCORE_TYPES:
                state.unsupported += 1
                state.unsupported_types.add(rtype)
            continue
        version = _check_version(record.get("version"), where)
        if sid is None:
            raise BridgeReadError(
                "unsupported-format",
                f"{where} (Claude Code {version}) has no session id.",
            )
        raw_uuid = record.get("uuid")
        if not isinstance(raw_uuid, str) or not UUID_RE.fullmatch(raw_uuid):
            raise BridgeReadError(
                "unsupported-format", f"{where} (Claude Code {version}) has no valid uuid."
            )
        uuid: str = raw_uuid.replace("-", "").lower()
        parent = record.get("parentUuid") or record.get("logicalParentUuid")
        parent = parent.replace("-", "").lower() if isinstance(parent, str) and parent else None
        state.parent_of[uuid] = parent
        timestamp = record.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) else None
        rec_agent = record.get("agentId") if isinstance(record.get("agentId"), str) else None
        agent = agent_id or rec_agent if sidechain else None
        if not sidechain:
            state.last_main_uuid = uuid

        emitted: list[_Rec] = []

        def add(role: str, origin: str, text: str, **extra: Any) -> None:
            emitted.append(
                _Rec(
                    uuid=uuid,
                    parent_uuid=parent,
                    agent_id=agent,
                    timestamp=timestamp,
                    role=role,
                    origin=origin,
                    text=text,
                    orig_bytes=len(text.encode("utf-8")),
                    **extra,
                )
            )

        if rtype == "system":
            content = record.get("content")
            if content is not None and not isinstance(content, str):
                raise BridgeReadError(
                    "unsupported-format", f"{where} (Claude Code {version}) has malformed system content."
                )
            subtype = record.get("subtype")
            text = content or (str(subtype) if subtype else "")
            if subtype == "compact_boundary" and not content:
                text = "Conversation compacted"
            add("system", "agent" if sidechain else "meta", text)
        else:
            message = record.get("message")
            if not isinstance(message, dict) or "content" not in message:
                raise BridgeReadError(
                    "unsupported-format",
                    f"{where} (Claude Code {version}) has no message content.",
                )
            for block in _blocks(message["content"], where):
                btype = block.get("type")
                if btype in REASONING_BLOCKS:
                    continue
                if rtype == "assistant":
                    origin = "agent" if sidechain else "assistant"
                    if btype == "text":
                        add("assistant", origin, _text_field(block, where, version))
                    elif btype == "tool_use":
                        name = block.get("name")
                        if not isinstance(name, str) or not name:
                            raise BridgeReadError(
                                "unsupported-format",
                                f"{where} (Claude Code {version}) has a tool_use block without a name.",
                            )
                        tool_input = block.get("input", {})
                        if not isinstance(tool_input, (dict, list, str)):
                            raise BridgeReadError(
                                "unsupported-format",
                                f"{where} (Claude Code {version}) has a malformed tool_use input.",
                            )
                        tool_use_id = block.get("id")
                        tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None
                        full_input = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                        text, clipped = _clip(full_input, TOOL_INPUT_CAP)
                        if tool_use_id:
                            state.tool_names[tool_use_id] = name
                        add(
                            "assistant",
                            origin,
                            text,
                            tool=name,
                            tool_use_id=tool_use_id,
                            truncated=clipped,
                        )
                        emitted[-1].orig_bytes = len(full_input.encode("utf-8"))
                        if emitted[-1:] and tool_use_id:
                            state.tool_use_uuid.setdefault(tool_use_id, uuid)
                else:  # user role
                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None
                        raw_result = block.get("content")
                        if raw_result is not None and not isinstance(raw_result, (str, list)):
                            raise BridgeReadError(
                                "unsupported-format",
                                f"{where} (Claude Code {version}) has malformed tool_result content.",
                            )
                        if isinstance(raw_result, list) and not all(
                            isinstance(item, str)
                            or (isinstance(item, dict) and (item.get("type") != "text" or isinstance(item.get("text"), str)))
                            for item in raw_result
                        ):
                            raise BridgeReadError(
                                "unsupported-format",
                                f"{where} (Claude Code {version}) has malformed tool_result content.",
                            )
                        full = _result_text(raw_result)
                        text, clipped = _clip(full, TOOL_RESULT_READ_CAP)
                        add(
                            "tool_result",
                            "agent" if sidechain else "assistant",
                            text,
                            tool=state.tool_names.get(tool_use_id or ""),
                            tool_use_id=tool_use_id,
                            is_error=block.get("is_error") is True,
                            truncated=clipped,
                        )
                        emitted[-1].orig_bytes = len(full.encode("utf-8"))
                        result = record.get("toolUseResult")
                        if (
                            tool_use_id
                            and isinstance(result, dict)
                            and isinstance(result.get("agentId"), str)
                        ):
                            state.agent_tool_use[result["agentId"]] = tool_use_id
                    elif btype == "text":
                        text = _text_field(block, where, version)
                        add("user", _user_origin(record, text, sidechain), text)
        for index, rec in enumerate(emitted):
            if index:
                rec.primary_uuid = rec.uuid
                digest = hashlib.sha256(f"{rec.uuid}:{index}".encode()).hexdigest()
                rec.uuid = digest
            rec.order = state.counter
            state.counter += 1
            state.records.append(rec)


# --------------------------------------------------------------------------
# Refs and budget
# --------------------------------------------------------------------------


def _assign_refs(records: list[_Rec]) -> dict[int, str]:
    """Stable refs: a UUID prefix of at least 8 hex characters.

    Records are allocated in reading order. A record takes the shortest prefix
    that is not a prefix of any earlier record's UUID, so a record appended
    later can only lengthen its own ref and never changes an existing one.
    """
    seen: set[str] = set()
    by_uuid: dict[str, str] = {}
    for rec in records:
        if rec.uuid in by_uuid:
            continue
        uuid = rec.uuid
        ref = uuid
        for length in range(min(MIN_REF_LENGTH, len(uuid)), len(uuid) + 1):
            if uuid[:length] not in seen:
                ref = uuid[:length]
                break
        by_uuid[uuid] = ref
        for length in range(min(MIN_REF_LENGTH, len(uuid)), len(uuid) + 1):
            seen.add(uuid[:length])
    return {id(rec): by_uuid[rec.uuid] for rec in records}


def _record_dict(rec: _Rec, ref: str, parent_ref: str | None) -> dict[str, Any]:
    return {
        "ref": ref,
        "parentRef": parent_ref,
        "agentId": rec.agent_id,
        "timestamp": rec.timestamp,
        "role": rec.role,
        "origin": rec.origin,
        "text": rec.text,
        "tool": rec.tool,
        "toolUseId": rec.tool_use_id,
        "isError": rec.is_error,
        "truncated": rec.truncated,
    }


def render(payload: dict[str, Any]) -> str:
    """The exact text ``helmet bridge read`` prints, newline included."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _excerpt(text: str) -> str:
    if len(text) <= EXCERPT_HEAD + EXCERPT_TAIL:
        return text
    omitted = len(text) - EXCERPT_HEAD - EXCERPT_TAIL
    return f"{text[:EXCERPT_HEAD]}\n…[{omitted} characters omitted]…\n{text[-EXCERPT_TAIL:]}"


def _sort_key(rec: _Rec) -> tuple[str, int]:
    return (rec.timestamp or "", rec.order)


def read_session(
    session_id: str,
    *,
    transcript: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    claude_home: Path | None = None,
) -> dict[str, Any]:
    """Return the ``hermes-helmet.bridge.records/1`` payload or raise."""
    if transcript is None:
        home = claude_home if claude_home is not None else claude_config_dir()
        path = find_session_file(session_id, home / "projects")
    else:
        path = Path(transcript)
        if not path.is_file():
            raise BridgeReadError(
                "session-not-found", f"Transcript {path.name} does not exist or is not a file."
            )
    state = _State()
    _scan_file(path, session_id, state, agent_id=None)
    main_count = len(state.records)
    for sub in _subagent_files(path, session_id):
        agent = sub.stem[len("agent-") :] if sub.stem.startswith("agent-") else sub.stem
        _scan_file(sub, session_id, state, agent_id=agent)
    if main_count == 0:
        raise BridgeReadError(
            "unsupported-format",
            "The transcript has no conversation records this reader recognizes.",
        )

    records = sorted(state.records, key=_sort_key)
    refs = _assign_refs(records)
    ref_by_uuid: dict[str, str] = {}
    for rec in records:
        key = rec.primary_uuid or rec.uuid
        ref_by_uuid.setdefault(key, refs[id(rec)])
        ref_by_uuid.setdefault(rec.uuid, refs[id(rec)])
    first_ref_of_agent: dict[str, str] = {}
    for rec in records:
        if rec.agent_id:
            first_ref_of_agent.setdefault(rec.agent_id, refs[id(rec)])

    def parent_ref_for(rec: _Rec, included: set[str] | None) -> str | None:
        if rec.primary_uuid:
            return ref_by_uuid.get(rec.primary_uuid)
        current = rec.parent_uuid
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            candidate = ref_by_uuid.get(current)
            if candidate and (included is None or candidate in included):
                return candidate
            current = state.parent_of.get(current)
        if rec.agent_id and rec.parent_uuid is None:
            tool_use_id = state.agent_tool_use.get(rec.agent_id)
            origin_uuid = state.tool_use_uuid.get(tool_use_id or "")
            if origin_uuid:
                candidate = ref_by_uuid.get(origin_uuid)
                if candidate and (included is None or candidate in included):
                    return candidate
        return None

    total = len(records)
    protected = {"person", "assistant", "agent"}
    original_bytes = sum(rec.orig_bytes for rec in records)
    last_uuid = state.last_main_uuid or (records[-1].primary_uuid or records[-1].uuid)
    texts = {id(rec): rec.text for rec in records}
    truncated = {id(rec): rec.truncated for rec in records}
    extra_warnings: list[dict[str, str]] = []
    if state.unsupported:
        extra_warnings.append(
            {
                "code": "unsupported-records-skipped",
                "message": (
                    f"Skipped {state.unsupported} records of unknown type: "
                    + ", ".join(sorted(state.unsupported_types))
                    + "."
                ),
            }
        )
    if state.pending_tail:
        extra_warnings.append(
            {
                "code": "pending-tail",
                "message": "An incomplete final line was ignored; Claude Code may still be writing.",
            }
        )

    def build(keep: list[_Rec], budget_warnings: list[dict[str, str]]) -> dict[str, Any]:
        included = {refs[id(rec)] for rec in keep}
        out_records = []
        for rec in keep:
            item = _record_dict(rec, refs[id(rec)], parent_ref_for(rec, included))
            item["text"] = texts[id(rec)]
            item["truncated"] = truncated[id(rec)]
            out_records.append(item)
        final_bytes = sum(len(item["text"].encode("utf-8")) for item in out_records)
        return {
            "schema": SCHEMA,
            "helmetVersion": __version__,
            "sessionId": session_id,
            "readAt": _utc_now(),
            "fingerprint": f"{total}:{last_uuid}",
            "records": out_records,
            "coverage": {
                "recordsTotal": total,
                "recordsIncluded": len(out_records),
                "bytesOmitted": max(0, original_bytes - final_bytes),
                "unsupportedSkipped": state.unsupported,
                "pendingTail": state.pending_tail,
            },
            "warnings": budget_warnings + extra_warnings,
        }

    def measure(keep: list[_Rec]) -> int:
        return len(render(build(keep, [])).encode("utf-8"))

    keep = list(records)
    budget_warnings: list[dict[str, str]] = []
    if measure(keep) > max_bytes:
        for rec in keep:
            if rec.role == "tool_result" and len(texts[id(rec)]) > EXCERPT_HEAD + EXCERPT_TAIL:
                texts[id(rec)] = _excerpt(texts[id(rec)])
                truncated[id(rec)] = True
        # Drop the oldest tool results until the serialized output fits.
        droppable = [rec for rec in records if rec.role == "tool_result"]
        low, high = 0, len(droppable)  # smallest count of oldest results to drop
        while low < high:
            mid = (low + high) // 2
            gone = {id(rec) for rec in droppable[:mid]}
            if measure([rec for rec in records if id(rec) not in gone]) <= max_bytes:
                high = mid
            else:
                low = mid + 1
        gone = {id(rec) for rec in droppable[:low]}
        keep = [rec for rec in records if id(rec) not in gone]
        running = measure(keep)
        if running > max_bytes:
            protected_bytes = sum(
                len(texts[id(rec)].encode("utf-8"))
                for rec in keep
                if rec.origin in protected and rec.role in {"user", "assistant"}
            )
            budget_warnings.append(
                {
                    "code": "budget-conflict",
                    "message": (
                        f"Person prompts and assistant text ({protected_bytes} bytes) are "
                        f"protected and are returned in full, so the output is {running} "
                        f"bytes, over --max-bytes {max_bytes}."
                    ),
                }
            )
    return build(keep, budget_warnings)
