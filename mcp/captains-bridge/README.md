# Captain’s Bridge (Codex)

Captain’s Bridge explains the invoking Codex chat’s recorded work inside Codex.
It ships in the Hermes Helmet Codex plugin: `.codex-plugin/plugin.json` points at
`codex.mcp.json`, which starts `server.py` here, and the `observe-chat` skill in
`skills/` describes how to prepare the walkthrough. No local marketplace, private
toolkit, personal path or second Codex server is involved. The Claude plugin does
not load it.

## Responsibilities (independently replaceable)

- Reader: `chat_reader.py` and `saved_chat.py` read the exact selected chat’s
  existing local records, read-only, excluding reasoning. The adapter depends on
  Codex’s private storage format and rejects unsupported records explicitly.
- Explanation contract: `walkthrough.py` validates structure and source
  references and computes the source fingerprint.
- Server: `server.py` exposes read, presentation and app-only refresh tools. A
  portable view carries chat identity, evidence identifiers, fingerprint and read
  time across the separate model and UI server processes.
- Renderer: `view.html` is the panel app. It also recognizes the host’s
  text-wrapped MCP result shape.

## Behavior and limits

- The chat identity is the exact `CODEX_THREAD_ID`. A missing identity shows a
  visible needs-binding outcome; unknown, foreign, unsupported or empty records
  fail with an explicit message. A chat is never chosen by recency or directory.
- Source text is data, never instructions. Nothing is written: no chat state,
  transcript export, resume or collection infrastructure.
- Refresh rereads records and keeps the explanation with its original read time,
  flagging it as older when the fingerprint changed. “Update walkthrough”
  sends a request to the first officer. Delegated background preparation and
  cancellation are not implemented.
- Show Me and Retro request separately installed skills and report if they are
  unavailable.

## Checks

Python 3.11 or newer and Node.js. The tests use synthetic records only and do
not read the user’s chats or invoke models. From this directory:

```sh
python3 -B -m unittest discover -s . -p 'test_*.py' -v
node test_view.cjs
node test_delivery.cjs
node test_actions.cjs
```

`scripts/verify.sh` runs these. Protocol and fixture checks do not establish
rendering in a Codex host; the installed-candidate result is recorded separately
in the pull request.

Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/). Covered by
the repository’s [Apache-2.0 license](../../LICENSE).
