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
- Refresh records calls the app-only read-only tool directly (no agent message,
  background work or project task), keeps the explanation with its original
  read time and flags it as older when the fingerprint changed. Failures,
  foreign or out-of-order responses and a partially written final record keep
  the last view and report the limit. “Update walkthrough”
  sends a request to the first officer. Delegated background preparation and
  cancellation are not implemented.
- Show Me (the chat or a selected work item) and Retro (session suggestions)
  are optional. Each is sent only by an explicit click, as one user message to
  the originating chat carrying its exact `CODEX_THREAD_ID`; the skill must stop
  on a mismatch. A second click is ignored while one is pending. “Delivered to
  this chat” means only that Codex accepted the message, not that the skill ran
  or finished. Failures keep the Bridge view. Retro only proposes; neither
  action implements or resumes project work. Walkthrough excerpts sent with a
  selected item are reference data to verify against the original records.
  Generated explanations belong outside tracked project files.

## Optional skill setup (Show Me and Retro)

Install the skills you want from their own owners and follow their licenses;
Hermes Helmet does not bundle or fork them. Codex discovers a skill named
`show-me` or `retro` from `$CODEX_HOME/skills` (default `~/.codex/skills`), the
repository’s `.agents/skills`, or an installed plugin’s `skills/`. No private
toolkit or personal path is needed. If a skill is missing, the chat says so and
stops without substituting another skill or imitating it; the rest of the
Bridge is unaffected.

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
