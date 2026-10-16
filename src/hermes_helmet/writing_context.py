#!/usr/bin/env python3
"""Fresh-context policy for writing-workflow Kanban cards.

Native Hermes assembles a worker's task context in ``build_worker_context`` and
always appends ``## Recent work by @<assignee>``: summaries of that profile's
last completed runs on *other* tasks. No flag turns it off, and a new session id
does not isolate it. The worker reads the context through the ``kanban_show``
tool result, so the supported seam is a native Hermes plugin on the
``transform_tool_result`` hook. It drops only that section, and only for cards
that carry the writing-card marker, so every other card is untouched.

What this module cannot do, and says so (see ``docs/helmet-write.md``):

- Built-in memory (``MEMORY.md``/``USER.md``) and an external memory provider are
  profile-level in Hermes, not per task. :func:`profile_findings` reports when a
  writing profile still loads them. The adopter owns that configuration.
- Hermes hooks fail open. A missing or disabled plugin leaks role history, which
  :func:`profile_findings` also reports.

Nothing here edits a vendor tree, deletes memory, or adds a service.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

CARD_MARKER = "<!-- helmet-write:card:v1 -->"
PLUGIN_NAME = "helmet-writing-context"

_ROLE_HISTORY = re.compile(r"^## Recent work by @[^\n]*\n.*?(?=^## |\Z)", re.M | re.S)


def strip_role_history(worker_context: str) -> str:
    """Remove native "Recent work by @profile" while keeping every other section."""

    stripped = _ROLE_HISTORY.sub("", worker_context)
    return stripped.rstrip() + "\n" if stripped != worker_context else worker_context


def transform_tool_result(
    tool_name: str = "", args: Any = None, result: Any = None, **_: Any
) -> str | None:
    """``transform_tool_result`` hook: ``None`` leaves the result unchanged."""

    if tool_name != "kanban_show" or not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    context = payload.get("worker_context")
    task = payload.get("task")
    if not isinstance(context, str) or not isinstance(task, dict):
        return None
    if CARD_MARKER not in str(task.get("body") or ""):
        return None
    cleaned = strip_role_history(context)
    if cleaned == context:
        return None
    payload["worker_context"] = cleaned
    return json.dumps(payload)


def register(context: Any) -> None:
    """Native plugin entry point."""

    context.register_hook("transform_tool_result", transform_tool_result)


def plugin_files() -> dict[str, str]:
    """Files for a native plugin directory; the adopter installs and enables it."""

    source = str(Path(__file__).resolve().parent.parent)
    return {
        "plugin.yaml": (
            f"name: {PLUGIN_NAME}\n"
            "version: 1.0.0\n"
            "description: Drop unrelated role history from writing-card task context\n"
            "provides_hooks:\n"
            "  - transform_tool_result\n"
        ),
        "__init__.py": (
            "# Managed by Hermes Helmet (helmet write context-plugin)\n"
            "import sys\n"
            f"sys.path.insert(0, {source!r})\n"
            "from hermes_helmet.writing_context import register\n"
        ),
    }


def write_plugin(plugins_dir: str | Path) -> Path:
    """Write the plugin under ``PLUGINS_DIR/helmet-writing-context``."""

    target = Path(plugins_dir) / PLUGIN_NAME
    target.mkdir(parents=True, exist_ok=True)
    for name, text in plugin_files().items():
        (target / name).write_text(text, encoding="utf-8")
    return target


def _truthy(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def profile_findings(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Gaps that keep a profile from giving a writing card a fresh context.

    An empty list means the profile configuration supplies no persistent
    retrieval and enables the role-history plugin. It is a configuration check,
    not a per-task guarantee; ``helmet write context`` shows the effective input.
    """

    findings: list[dict[str, str]] = []
    memory = config.get("memory") if isinstance(config, Mapping) else None
    memory = memory if isinstance(memory, Mapping) else {}
    if _truthy(memory.get("memory_enabled")):
        findings.append({"code": "builtin_memory_enabled", "fix": "memory.memory_enabled: false"})
    if _truthy(memory.get("user_profile_enabled")):
        findings.append(
            {"code": "user_profile_enabled", "fix": "memory.user_profile_enabled: false"}
        )
    provider = memory.get("provider")
    if isinstance(provider, str) and provider.strip():
        findings.append({"code": "external_memory_provider", "fix": "memory.provider: ''"})
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    plugins = plugins if isinstance(plugins, Mapping) else {}
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    if not isinstance(enabled, list) or PLUGIN_NAME not in enabled or (
        isinstance(disabled, list) and PLUGIN_NAME in disabled
    ):
        findings.append(
            {"code": "context_plugin_not_enabled", "fix": f"hermes plugins enable {PLUGIN_NAME}"}
        )
    return findings
