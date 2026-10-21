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
  references and computes the source fingerprint. `walkthrough_contract.py` is
  the one source of its limits, counts and enums: validation enforces them and
  `tools/list` publishes them as the `walkthrough` schema of `present_chat_work`
  and `deliver_walkthrough_update`.
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
- Show Me (the chat or a selected work item) and Retro (session suggestions)
  are optional. Each is sent only by an explicit click, as one user message to
  the originating chat carrying its exact `CODEX_THREAD_ID`; the skill must stop
  on a mismatch. A second click is ignored while one is pending. “Delivered to
  this chat” means only that Codex accepted the message, not that the skill ran
  or finished. Failures keep the Bridge view. Retro only proposes; neither
  action implements or resumes project work. Walkthrough excerpts sent with a
  selected item are reference data to verify against the original records.
  Generated explanations belong outside tracked project files.

## Walkthrough limits and errors

Read the nested limits from the `walkthrough` property in `tools/list`; do not
learn them by failing a request. Text limits count characters of the submitted
string before trimming, and invalid text is rejected, never shortened. Item,
evidence and entry counts and the enums are in the schema. Only the leading
`steps`, `relations` and `links` listed there are processed; later entries are
ignored rather than rejected, so the schema constrains exactly those leading
positions (`prefixItems`) and leaves the rest unconstrained. An optional
`reported`, `disposition` or `change` may also be `null`, `false`, `0`, `""`, `[]`
or `{}`, which count as absent, and a link URL may carry surrounding whitespace
that is trimmed. The schema never excludes a payload the runtime accepts; the
runtime additionally checks record IDs, that a URL occurs in a cited record, and
the size guard below. Extra properties are ignored. Separately,
`len(json.dumps(walkthrough))` must be at most 100000 before normalization:
that counts serialized characters with ASCII escaping, not UTF-8 bytes, so
non-ASCII text counts for more than its length.

An invalid payload returns a tool error naming the exact field with zero-based
array indices (for example `items[0].change.before`), the expected constraint
and, for text, the actual length. An evidence error names the owning field and
index without echoing source text.

`mcp/captains-bridge/example-walkthrough.json` is the one synthetic example. The
tests validate those exact bytes against the schema and the runtime; copy
nothing from this page instead of it.

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
to a panel, and the panel cannot tell whether the chat turn is still active, so
rendering in the original panel is unsupported on this route as measured. The panel
therefore ends the attempt at once: a request reports the boundary, captures no
snapshot, dispatches no agent and starts no timer, and the last view is kept. The
background lifecycle runs only when the host advertises
`hostCapabilities.backgroundWalkthroughDelivery`; the installed Codex host does not.
Agent completion alone does not establish a successful or validated delivery, so no
message claims one. The server-side delivery, provenance, ownership, cancellation and
Refresh behavior are unchanged, and no second queue, retry or observer was added.

Unsupported boundaries, reported rather than hidden: the server cannot itself stop
a subagent or know the Captain issued a new instruction; the first officer does
both from the messages above. Whether a delivery reaches the panel when the parent
turn has already finished, and whether work continues during preparation, depend
on Codex host behavior that synthetic tests cannot establish; they require the
installed-host acceptance recorded in the pull request.

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
node test_preparation.cjs
```

`scripts/verify.sh` runs these. Protocol and fixture checks do not establish
rendering in a Codex host; the installed-candidate result is recorded separately
in the pull request.

Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/). Covered by
the repository’s [Apache-2.0 license](../../LICENSE).
