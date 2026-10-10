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
  the last view and report the limit. “Update walkthrough” rereads only; see
  Background preparation below.
- Show Me and Retro request separately installed skills and report if they are
  unavailable.

## Background preparation lifecycle

“Update walkthrough” (and its stale notice) runs this lifecycle. Opening the
Bridge never starts it.

1. Capture: the panel calls `request_walkthrough_update` with its portable view.
   The server rereads that exact chat and returns a request: a random
   `requestId`, the `threadId`, and a snapshot (source fingerprint, read time,
   record count). No server-side state is kept, so nothing is process-local.
2. Dispatch: the panel sends one `ui/message` asking the first officer to start
   exactly one read-only subagent, not wait or poll, and not pause project work.
   The subagent reads only `threadId` with `read_chat_work`, never its own chat.
   It must not run project tasks or repair the viewer.
3. Deliver: the subagent calls `deliver_walkthrough_update` once with the request
   unchanged, or with `failure`. Citations are validated against the snapshot’s
   records only; the result keeps the snapshot’s fingerprint and read time, so a
   later chat change marks it older instead of fresh. The snapshot also carries a
   digest of the exact captured records and a seal over every snapshot field. On
   delivery the server rereads the chat and rejects the request if a captured
   record has changed, if the digests are inconsistent, or if the chat names no
   valid chat; records appended later are allowed and only mark the result older.
   The seal is a consistency check, not authentication: the server keeps no
   secret or state, so it does not stop a caller who can read the chat from
   building a consistent snapshot. It also does not preserve the captured text;
   it detects that the text changed.
4. Settle: the panel owns the single active request. It accepts a delivery only
   when `requestId` matches and the request is still active, and ignores every
   other result. Delivery, failure, cancel, supersession or the 10-minute timeout
   end the request; each preserves the last useful view, and a second delivery
   for a settled request is dropped. The panel never retries.
5. Cancel: “Cancel update” ends the request at once and asks the first officer to
   interrupt the subagent. If the host cannot be told, the panel says so and still
   discards any later result. The 10-minute timeout does the same: it invalidates
   the request at once, then asks the first officer to interrupt the subagent. The
   panel says the stop was requested, not confirmed, and says so plainly if the
   host could not be told. Each async effect (acknowledgement failure, timeout
   handoff) applies only to the request that owns it, so a late failure of a
   cancelled request never clears a newer one. A substantive new instruction from the Captain
   supersedes the request the same way unless the Captain says to keep it.

Measured host limit (installed Codex, parent turn already finished): the preparation
agent read the originating chat and `deliver_walkthrough_update` returned a validated
walkthrough with the captured fingerprint, read time and request identity, so delivery
to the server worked. The originating panel did not render it: it kept the old
explanation and its progress notice until its own 10-minute timeout, and interrupting
the agent reported it had already completed. This server keeps no state and cannot push
to a panel, so rendering in the original panel after the parent finishes is unsupported
by the host seam as measured; no background rendering is claimed. The panel therefore
says a result may not be shown if the chat turn has finished, offers Cancel to stop
waiting, and on timeout says a finished agent's result reached the server but cannot be
shown. A failure result is subject to the same boundary. Foreground delivery while the
parent turn is still active is unchanged. No second queue or observer was added.

Unsupported boundaries, reported rather than hidden: the server cannot itself stop
a subagent or know the Captain issued a new instruction; the first officer does
both from the messages above. Whether a delivery reaches the panel when the parent
turn has already finished, and whether work continues during preparation, depend
on Codex host behavior that synthetic tests cannot establish; they require the
installed-host acceptance recorded in the pull request.

## Checks

Python 3.11 or newer and Node.js. The tests use synthetic records only and do
not read the user’s chats or invoke models. From this directory:

```sh
python3 -B -m unittest discover -s . -p 'test_*.py' -v
node test_view.cjs
node test_delivery.cjs
node test_actions.cjs
node test_preparation.cjs
```

`scripts/verify.sh` runs these. Protocol and fixture checks do not establish
rendering in a Codex host; the installed-candidate result is recorded separately
in the pull request.

Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/). Covered by
the repository’s [Apache-2.0 license](../../LICENSE).
