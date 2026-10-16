#!/usr/bin/env python3
"""Ask a real Codex host which skills it discovers (app-server, stdio).

Usage: codex_skills_probe.py CODEX_EXECUTABLE WORKDIR

Starts a fresh ``codex app-server`` (a fresh session), sends the ``skills/list``
request with ``forceReload`` and prints ``{"skill name": "path to SKILL.md"}``
as JSON. It reads what the host itself reports, not the plugin files.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading


def main(argv: list[str]) -> int:
    codex, workdir = argv[1], argv[2]
    proc = subprocess.Popen(
        [codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, cwd=workdir,
    )
    lines: "queue.Queue[dict]" = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.strip().startswith("{"):
                lines.put(json.loads(line))

    threading.Thread(target=pump, daemon=True).start()
    counter = [0]

    def send(method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            counter[0] += 1
            message["id"] = counter[0]
        proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        if notify:
            return None
        while True:
            reply = lines.get(timeout=60)
            if reply.get("id") == counter[0]:
                return reply

    try:
        send("initialize", {"clientInfo": {"name": "helmet-skills-probe", "version": "1"}})
        send("initialized", notify=True)
        reply = send("skills/list", {"cwds": [workdir], "forceReload": True}) or {}
        found = {
            skill["name"]: skill["path"]
            for entry in reply.get("result", {}).get("data", [])
            for skill in entry.get("skills", [])
        }
        print(json.dumps(found))
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
