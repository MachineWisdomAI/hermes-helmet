#!/usr/bin/env python3
"""Capability smoke checks for the writing image. Standard library only.

    smoke.py documents [--skills-root DIR]
    smoke.py diagram   [--assets DIR]

``documents`` runs the *bundled* Hermes ``docx`` and ``powerpoint`` skill scripts
with the interpreter that runs this file (``sys.executable``), so run it once
per image interpreter. It creates, reads and edits a DOCX (body text and a
table cell) and a PPTX (slide order, text and speaker notes), and asserts the
result by reading the files back.

``diagram`` renders a fixture offline with the image's ``diagram`` skill and
checks: no parse error, every fixture label appears as SVG text, the PNG decodes
with positive dimensions and non-uniform content, and the Mermaid source is
retained. A broken Mermaid source must be rejected without writing outputs.

Nothing here installs a package or touches the network. Exit status 0 means
every check passed; the last stdout line is a one-line JSON summary.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

DEFAULT_SKILLS_ROOT = Path("/opt/hermes/skills/productivity")
DEFAULT_ASSETS = Path("/opt/hermes-helmet/writing")
FIXTURE_LABELS = ("Intake", "Draft", "Edit", "Publish", "reviewed")
FIXTURE_MERMAID = (
    "graph LR\n"
    "  A[Intake] -->|reviewed| B[Draft]\n"
    "  B --> C[Edit]\n"
    "  C --> D[Publish]\n"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SmokeFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def run_json(argv: list[str]) -> dict:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:] or [""]
        raise SmokeFailure(f"{Path(argv[1]).name} exited {completed.returncode}: {tail[0][:200]}")
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure(f"{Path(argv[1]).name} did not print JSON") from exc


def smoke_documents(skills_root: Path) -> dict:
    py = sys.executable
    docx_dir = skills_root / "docx" / "scripts"
    pptx_dir = skills_root / "powerpoint" / "scripts"
    for script in (
        docx_dir / "docx_create.py",
        docx_dir / "docx_read.py",
        docx_dir / "docx_edit.py",
        pptx_dir / "pptx_create.py",
        pptx_dir / "pptx_read.py",
        pptx_dir / "pptx_edit.py",
    ):
        require(script.is_file(), f"bundled skill script missing: {script.name}")

    with tempfile.TemporaryDirectory(prefix="helmet-writing-smoke-") as tmp:
        work = Path(tmp)

        spec = {
            "blocks": [
                {"type": "heading", "text": "Quarterly Plan", "level": 1},
                {"type": "paragraph", "text": "The owner is Dana."},
                {"type": "table", "header": ["Item", "Value"], "rows": [["Budget", "42"], ["Owner", "Dana"]], "style": "Table Grid"},
            ]
        }
        (work / "doc.json").write_text(json.dumps(spec), encoding="utf-8")
        run_json([py, str(docx_dir / "docx_create.py"), str(work / "doc.json"), str(work / "doc.docx")])
        created = run_json([py, str(docx_dir / "docx_read.py"), str(work / "doc.docx"), "--text"])
        require("Quarterly Plan" in created["body"], "docx: created heading was not read back")
        require(["Owner", "Dana"] in created["tables"][0], "docx: created table row was not read back")
        run_json(
            [py, str(docx_dir / "docx_edit.py"), "replace", str(work / "doc.docx"), "--find", "Dana", "--replace", "Priya", "-o", str(work / "edited.docx")]
        )
        edited = run_json([py, str(docx_dir / "docx_read.py"), str(work / "edited.docx"), "--text"])
        require("The owner is Priya." in edited["body"], "docx: edited paragraph was not read back")
        require(["Owner", "Priya"] in edited["tables"][0], "docx: edited table cell was not read back")
        require("Dana" not in json.dumps(edited), "docx: the replaced name is still present")

        deck = {
            "slide_size": "16:9",
            "slides": [
                {"layout": "title", "title": "Launch Review", "subtitle": "Q3", "notes": "Open with the goal."},
                {"layout": "title_content", "title": "Risks", "bullets": ["Schedule", "Budget"], "notes": "Mention Dana."},
            ],
        }
        (work / "deck.json").write_text(json.dumps(deck), encoding="utf-8")
        run_json([py, str(pptx_dir / "pptx_create.py"), str(work / "deck.json"), str(work / "deck.pptx")])
        outline = run_json([py, str(pptx_dir / "pptx_read.py"), str(work / "deck.pptx"), "--outline"])
        titles = [slide["texts"][0] for slide in outline["slides"]]
        require(titles == ["Launch Review", "Risks"], "pptx: slide order or titles were not read back")
        run_json(
            [
                py, str(pptx_dir / "pptx_edit.py"), str(work / "deck.pptx"),
                "--replace-text", "Dana", "Priya", "--append-notes", "1", "Confirm the date.",
                "--output", str(work / "edited.pptx"),
            ]
        )
        notes = run_json([py, str(pptx_dir / "pptx_read.py"), str(work / "edited.pptx"), "--notes"])["notes"]
        require(notes[0] == "Open with the goal.", "pptx: first speaker note changed")
        require(notes[1] == "Mention Priya.\nConfirm the date.", "pptx: edited speaker notes were not read back")

    return {"check": "documents", "python": sys.version.split()[0], "docx": "create/read/edit", "pptx": "create/read/edit+notes"}


def png_summary(data: bytes) -> tuple[int, int]:
    """Decode a PNG's pixel stream and return ``(width, height)``.

    Checks the signature, parses chunks, inflates the IDAT stream, and requires
    at least two distinct byte values, so an all-blank image is rejected. It
    does not unfilter scanlines: a blank white or transparent canvas still has
    near-uniform filtered bytes.
    """

    require(data[:8] == PNG_SIGNATURE and len(data) > 64, "png: not a PNG")
    offset, width, height, idat = 8, 0, 0, bytearray()
    while offset + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[offset : offset + 8])
        body = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        offset += 12 + length
    require(width > 0 and height > 0, "png: non-positive dimensions")
    pixels = zlib.decompress(bytes(idat))
    require(len(pixels) >= height, "png: pixel stream is shorter than its height")
    require(len(set(pixels)) > 2, "png: image has no content")
    return width, height


def svg_text(svg: str) -> str:
    return " ".join(re.findall(r">([^<>]+)<", svg))


def smoke_diagram(assets: Path) -> dict:
    render = assets / "skills" / "diagram" / "scripts" / "render.mjs"
    require(render.is_file(), "diagram: render.mjs is missing from the image")
    check = subprocess.run(["node", str(render), "--check"], capture_output=True, text=True, timeout=60, check=False)
    require(check.returncode == 0, "diagram: --check failed: " + (check.stdout or check.stderr).strip()[:300])
    state = json.loads(check.stdout.strip().splitlines()[-1])
    require(state.get("ok") is True, "diagram: capability reports missing prerequisites")

    with tempfile.TemporaryDirectory(prefix="helmet-diagram-smoke-") as tmp:
        work = Path(tmp)
        source = work / "flow.mmd"
        source.write_text(FIXTURE_MERMAID, encoding="utf-8")
        rendered = subprocess.run(
            ["node", str(render), str(source), "--out-dir", str(work), "--slug", "flow"],
            capture_output=True, text=True, timeout=180, check=False,
        )
        require(rendered.returncode == 0, "diagram: render failed: " + (rendered.stderr or rendered.stdout).strip()[:300])
        require("Parse error" not in rendered.stdout + rendered.stderr, "diagram: parse error")
        require("PAGE_ERRORS" not in rendered.stdout, "diagram: page errors were reported")
        require(source.read_text(encoding="utf-8") == FIXTURE_MERMAID, "diagram: Mermaid source was not retained")
        text = svg_text((work / "flow.svg").read_text(encoding="utf-8"))
        missing = [label for label in FIXTURE_LABELS if label not in text]
        require(not missing, "diagram: SVG text is missing labels: " + ", ".join(missing))
        width, height = png_summary((work / "flow.png").read_bytes())
        require(not (work / "flow.excalidraw").exists(), "diagram: unexpected Excalidraw output")

        broken = work / "broken.mmd"
        broken.write_text("graph LR\n  A[ -->\n", encoding="utf-8")
        failed = subprocess.run(
            ["node", str(render), str(broken), "--out-dir", str(work / "broken-out"), "--slug", "broken"],
            capture_output=True, text=True, timeout=180, check=False,
        )
        require(failed.returncode != 0 and "Parse error" in failed.stderr, "diagram: a broken source was not rejected")
        require(not (work / "broken-out").exists(), "diagram: a broken source still wrote outputs")

    return {"check": "diagram", "png": [width, height], "labels": list(FIXTURE_LABELS), "gstack_commit": state.get("gstack_commit")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Writing image capability smoke checks.")
    sub = parser.add_subparsers(dest="check", required=True)
    docs = sub.add_parser("documents")
    docs.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    diagram = sub.add_parser("diagram")
    diagram.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    args = parser.parse_args(argv)
    try:
        summary = smoke_documents(args.skills_root) if args.check == "documents" else smoke_diagram(args.assets)
    except SmokeFailure as exc:
        print(json.dumps({"ok": False, "check": args.check, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
