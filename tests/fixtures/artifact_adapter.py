#!/usr/bin/env python3
"""Isolated stand-in for a deployment's artifact adapter (test fixture only).

It implements the three ``artifact-*`` verbs of docs/task-artifacts.md over a
temporary directory, mirroring the semantics of the native Kanban attachment
store the real adapter reaches: per-task directories, a safe base name, a
collision-free stored name, the 25 MiB per-file cap, task-scoped lookup, and
rows that expose a worker-side ``stored_path``. It never touches a real board.

Layout under ``$ARTIFACT_FIXTURE_ROOT``: ``<board>/<task>/`` holds blobs and
``<board>/<task>/.rows.json`` holds the metadata rows. A task exists once its
directory does.

Test-only modes (``$ARTIFACT_FIXTURE_MODE``) simulate a failing adapter, and the
``emit`` / ``sleep`` verbs exercise output bounds and timeouts.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

CAP = 25 * 1024 * 1024
NOT_FOUND, TOO_LARGE, INTEGRITY = 3, 4, 5


def safe_name(raw: str) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable()).strip().lstrip(".").strip()
    if not name:
        raise SystemExit(2)
    return name[:200]


def collision_free(directory: Path, name: str) -> str:
    stem, dot, ext = name.partition(".")
    candidate, n = name, 1
    while (directory / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return candidate


def load_rows(directory: Path) -> list[dict]:
    path = directory / ".rows.json"
    return json.loads(path.read_text()) if path.exists() else []


def option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def main(argv: list[str]) -> int:
    mode = os.environ.get("ARTIFACT_FIXTURE_MODE", "")
    if mode == "fail":
        # A real adapter might print credentials while failing; callers must
        # never surface this stream.
        sys.stderr.write("diagnostic credential-sentinel-7f3a\n")
        sys.stdout.write("credential-sentinel-7f3a")
        return 9
    verb = argv[0]
    if verb == "emit":
        sys.stdout.buffer.write(b"x" * int(argv[1]))
        return 0
    if verb == "sleep":
        time.sleep(float(argv[1]))
        return 0
    root = Path(os.environ["ARTIFACT_FIXTURE_ROOT"])
    board, task = argv[1], argv[2]
    directory = root / board / task
    if not directory.is_dir():
        return NOT_FOUND
    rows = load_rows(directory)
    if verb == "artifact-list":
        out = []
        for row in rows:
            blob = Path(row["stored_path"]).read_bytes()
            out.append({**row, "sha256": hashlib.sha256(blob).hexdigest()})
        if mode == "list-other-task":
            for row in out:
                row["task_id"] = "t_other"
        if mode == "list-no-checksum":
            for row in out:
                row.pop("sha256")
        sys.stdout.write(json.dumps(out))
        return 0
    if verb == "artifact-download":
        wanted = int(argv[3])
        for row in rows:
            if row["id"] == wanted:
                sys.stdout.buffer.write(Path(row["stored_path"]).read_bytes())
                return 0
        return NOT_FOUND
    if verb == "artifact-upload":
        data = sys.stdin.buffer.read(CAP + 1)
        if len(data) > CAP:
            return TOO_LARGE
        declared = int(option(argv, "--size"))
        digest = hashlib.sha256(data).hexdigest()
        if declared != len(data) or option(argv, "--sha256") != digest:
            return INTEGRITY
        stored = collision_free(directory, safe_name(option(argv, "--name")))
        (directory / stored).write_bytes(data)
        # Native attachment ids are one global autoincrement across tasks.
        counter = root / ".next_id"
        next_id = int(counter.read_text()) if counter.exists() else 1
        counter.write_text(str(next_id + 1))
        row = {
            "id": next_id,
            "task_id": task,
            "filename": stored,
            "stored_path": str((directory / stored).resolve()),
            "content_type": option(argv, "--media-type"),
            "size": len(data),
            "uploaded_by": "coordinator",
            "created_at": 1_700_000_000 + len(rows),
        }
        (directory / ".rows.json").write_text(json.dumps(rows + [row]))
        sys.stdout.write(json.dumps({**row, "sha256": digest}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
