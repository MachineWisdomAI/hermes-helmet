# Hermes Helmet Captain's Bridge: from a recorded chat to a cited walkthrough

Captain's Bridge is the part of Hermes Helmet that answers a plain question: what did the work in this chat actually come to? The Captain asks the first officer to open the Bridge. The first officer reads the chat's saved records, writes an account of the work that cites those records, and the Bridge shows that account in a panel. This document follows one hypothetical chat through that path and says, at each step, what the code checks and what it leaves to a person.

This is a retrospective description of the shipped Bridge at source revision [`b0daeb4`](https://github.com/MachineWisdomAI/hermes-helmet/tree/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c), not a design proposal. It does not claim the design was approved before or after it was built. Who the Captain, first officer and Hermes are, and whose credentials each uses, is covered in the [identities and delegated authority Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/docs/blueprints/identities-and-delegated-authority.md). The review and merge record that the Bridge does not replace is described in the [issue-delivery Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/docs/blueprints/issue-delivery-and-repair.md). Neither is repeated here.

## The example chat

Suppose you have been working in one Codex chat. Early on you asked for a polling interval to be shortened from 30 seconds to 10. The first officer handed the change to a Hermes worker. Hermes applied the polling change, and the first officer read the result and later reported the change complete with a pull request link. Nobody has yet recorded a review of that pull request.

This is the shape of the synthetic example that ships with the Bridge in [`example-walkthrough.json`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/example-walkthrough.json) and its tests. The chat is invented for this description; nothing below was run against a real chat.

## Three kinds of statement

Three kinds of statement end up in the Bridge, and the failures later in this document are easier to follow if you keep them apart.

- **Recorded facts** are the saved chat records themselves, each with an identifier: what the Captain asked, what command ran, what exit code came back, what the report said.
- **Generated interpretation** is the walkthrough the first officer writes from those records: that they add up to a change, that a handoff led to a result, that an item is unresolved. A model writes it, and the code does not know whether it is right.
- **Human or source judgment** is acceptance: whether a reported result was accepted, or whether work was delivered. The [observe-chat skill](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/skills/observe-chat/SKILL.md) says a source-bound explanation is not independent verification of delivery, and the `present_chat_work` tool description calls it an interpretation of records, not independent validation.

The Bridge checks that each citation names a record in this chat. As the skill puts it, the view "validates reference membership, not semantic truth". It does not detect architectural drift.

## Which host does what

Each host reads its own records in its own way, and the two are not feature-equal.

In Codex, the Bridge is an MCP panel. The [Codex plugin manifest](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/.codex-plugin/plugin.json) points at `codex.mcp.json`, which the [README](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/README.md) says starts `server.py`. The chat identity is the exact `CODEX_THREAD_ID`. The README states that the Claude plugin does not load this server. Most of this document describes the Codex panel.

Claude Code takes a distinct, textual path. The [Claude plugin manifest](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/.claude-plugin/plugin.json) describes a "Captain's Bridge mod" that opens a pane with `/captains-bridge`, and it exposes settings for the `helmet` command, the explanation model, a per-update time limit (default 180 seconds, 10 to 900) and the Show Me and Retro command names. The reader behind it is [`claude_bridge.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/src/hermes_helmet/claude_bridge.py), reached as `helmet bridge read`. It takes a session id, finds the one transcript named exactly `<session id>.jsonl`, and reads that file and the session's own subagent transcripts. It opens them for reading only, launches no Claude Code process and exports nothing. Its [tests](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/tests/test_claude_bridge_read.py) were read; the pane itself was not inspected. No claim is made here about how the Claude pane handles Update or cancellation, and the lifecycle described below is documented only for the Codex panel. Whether either plugin is installed or active on your machine is not something source can show.

## Four parts with separate jobs

The README names four responsibilities that can be replaced independently.

The reader, `chat_reader.py` and `saved_chat.py`, opens one chat's existing local records, read-only, and leaves out reasoning. The explanation contract, `walkthrough.py` and `walkthrough_contract.py`, checks the structure and citations of an account and computes a fingerprint of the source. The server, `server.py`, exposes the tools that read, present and refresh, and carries a portable view between the model's process and the panel's. The renderer, `view.html`, is the panel itself.

The first officer sits outside all four. It is the coding agent that reads the records, decides what they mean and writes the account. The code handles identity, provenance and shape; the meaning is the first officer's judgment.

## Step 1: binding to one exact chat

The skill tells the first officer to read `CODEX_THREAD_ID` from its own execution environment and never to infer the chat from a title, directory, recency, a referenced chat or a document. The first officer then calls `read_chat_work` with that identifier and `kind: messages`.

The instruction to use the environment variable lives in the skill and in a tool description. The server code does not read the variable itself, so the server cannot tell whether the identifier it receives came from the current chat. What the code does enforce is narrower. The reader accepts only a UUID-shaped identifier. It looks that identifier up in Codex's existing local chat index, which it opens read-only, and fails with an explicit message if the chat is not there, rather than falling back to the most recent chat. It then reads the chat's saved record file and requires that file to sit under Codex's session or archived-session directories. It requires the file's own session identity to match the requested identifier, and it refuses any event inside the file that names a different chat. Tests in [`test_chat_reader.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_chat_reader.py) cover the unknown chat, the wrong session identity and the foreign event, and a test checks that reading leaves every file under the Codex home unchanged and does not start another Codex server.

If no identifier arrives when the Bridge is opened, the server returns a visible "needs a binding" outcome instead of guessing.

## Step 2: reading the work

On a successful first call, the server reads the whole chat once, keeps the result in process memory under a random binding token, and returns that token with the first page. The same token must be used for later pages. If the server process has gone away, the token is unknown, and the error tells the first officer to open the Bridge again from its chat.

What the first officer receives is a list of records, each with an ID, an actor, a kind, its text and a flag saying whether the text was cut. The reader builds them from the saved completed items:

- Messages, in `kind: messages`: the Captain's requests, the first officer's progress messages and its final reports.
- Operations, in `kind: operations`: commands with their output and exit code, results of other tools, file changes, and explicit handoffs to other agents.

Pages hold up to 40 records, are limited to roughly 36,000 characters of text, and carry a `nextOffset` while more remain. The skill tells the first officer to read the meaningful messages across all turns first, then the operations that support any handoff, change, review or result it intends to claim.

Several details of the reader shape what can later be claimed. Record text is cut at 16,000 characters and marked truncated, and the skill says to resolve a truncated record from the original record or an explicitly linked source, or else narrow the claim. Reasoning records are excluded, and the skill adds that the first officer should not expose reasoning. Runtime context that Codex injects as a user message is not treated as a request from the Captain. The Bridge's own tool calls are left out, so the panel's payload is not copied back into the evidence. The actor on every non-Captain record is "First officer", because that is the chat's agent. Work done by a Hermes worker is not distinguishable in the records from work done by the first officer, which is why the contract has a separate rule for naming Hermes in a step.

The reader is candid about coverage. Its returned description says it covers Codex's saved turns and completed item records, and that live unfinished operations and worker-side Hermes events are not connected. The skill repeats the point: the extension does not collect Hermes Docker events, and missing coverage should be disclosed at the affected item. Worker results therefore appear only when they are recorded in the chat being read.

The format is private to Codex and the code treats it that way. A chat with only unrecognized item kinds, only excluded ones, or malformed records fails with a message. A chat with unrecognized kinds beside supported ones keeps its usable history. A half-written final line is ignored and flagged as a partial append, while a complete line that cannot be decoded is an error, so corruption is not silently dropped. The skill states the dependency plainly: the adapter relies on Codex's local storage format and is not a stable public API.

## Step 3: the first officer writes the account

With the records read, the first officer prepares a walkthrough: an objective, a summary and a list of work items, each with a group (`changed`, `unresolved` or `activity`), a status and a list of record IDs that support it. Items may carry steps with an actor, a before and after comparison, a reported-versus-disposition pair, explicit handoff or follow-up relations, and links.

In the example, the polling change becomes an item whose status is "Reported complete", not "Merged", because the chat only records a report. The open review becomes a separate `unresolved` item with the status "Open". The skill asks for exactly this attribution: a completed turn or a successful command is not delivery, and an outcome the records do not support is omitted.

Order is not cause. Steps establish a sequence, and the skill asks for causality only where a record states it. Handoff, wakeup and follow-up relations are for asynchronous relationships the chat explicitly records, not ones inferred from timing. Individual operations carry no timestamps in the saved format, so none are invented, and a gap is not treated as evidence of a stall.

## Step 4: the explanation contract

The first officer calls `present_chat_work` with the binding and the walkthrough. The server validates the walkthrough against the bound chat before anything is shown.

The limits and enums live in one place, `walkthrough_contract.py`. The same table drives the runtime check and the JSON Schema that the server publishes in `tools/list` for both `present_chat_work` and `deliver_walkthrough_update`. A test in [`test_walkthrough_contract.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_walkthrough_contract.py) confirms that the two tools publish identical schemas, that the published numbers match a written-out expectation, and that changing a limit in the shared table changes both the runtime error and the schema. The skill tells the first officer to read limits from the schema, not to learn them from errors. Among them: 1 to 24 items, 1 to 30 evidence IDs on any one claim, text caps per field, and a serialized size guard of 100,000 characters counted after ASCII escaping. Text over a limit is rejected, never shortened. Only the leading steps, relations and links are read, and the README states that later entries are ignored, not rejected.

Provenance is where the contract is strict. Every item, step, relation, reported or disposition pair, change and link must cite at least one record ID, and each ID must be one that `read_chat_work` returned for this bound chat. A foreign or missing ID fails with an error that names the field and array index without echoing the source text. Further rules apply:

- A step whose actor is Hermes must cite a record whose text mentions Hermes.
- Titles, step labels, relation labels and link labels must be readable names. Planning task IDs and chat UUIDs are rejected.
- A link's URL must be HTTPS, and it must appear verbatim in at least one record the link cites.
- Item IDs must be unique.

The same test file shows that the shipped example passes both the published schema and the runtime check and is accepted by both accepting tools, that foreign evidence is rejected, that a Hermes step without a Hermes record is rejected, and that error messages do not echo submitted source text.

Passing validation produces two stamps on the result. `explainedAt` is the time the chat was read, not the time the account was written. `fingerprint` is a SHA-256 hash of the normalized records the reader returned, plus each turn's ID, completion time and duration. Record text may already be truncated at the reader's 16,000-character cap, so the fingerprint does not cover the complete source chat: a change beyond the cap in a long record would not alter it. It covers that returned evidence, not the account.

### What validation proves and what it does not

The checks establish that each citation points at a record from this chat as it was read, that the shape and sizes are within limits, and that a quoted URL occurs in cited text. They do not establish that a cited record says what the account says it does. A first officer could cite a real record for a claim the record does not support, and the contract would accept it. The Hermes rule is a case-insensitive substring match, so it shows that Hermes is mentioned somewhere in a cited record, not that a Hermes worker did the step. The `reported` and `disposition` actor fields are free text. The skill puts the burden on the first officer: "you must actually check each cited record." In this revision nothing independent reads the records back against the account.

In the same way, the Bridge does not verify delivery. It does not look at the pull request, run the tests, or check that a merge happened. A status such as "Merged" appears only if the first officer chose to write it, and the skill asks it to attribute precisely: "Reported complete" for a worker's report, "Not accepted" where the Captain rejected, "Open" where nothing is finished.

## Step 5: the portable view and presentation

`present_chat_work` returns a short text line, "Walkthrough prepared for ... Rendering has not been verified.", and attaches the full payload for the panel. The text is deliberate. The skill says tool success is not proof of rendering and tells the first officer to inspect the actual panel when it can. If the panel stays at its connection screen, the first officer should report that failure and not claim the Bridge opened.

The payload is shaped as it is because of how Codex runs the server. Model tool calls and panel calls can reach independent server processes, so the panel cannot rely on the model process's memory. The server therefore builds a portable view: the chat's identifier and the validated explanation, with its fingerprint and read time. Everything the panel later asks for is derived from that view, and the server rereads the chat from the identifier each time. Nothing is written to disk: no transcript, no viewer file, no chat state. The view is served with empty connection and resource domain lists, so the panel declares no network access.

## Step 6: navigation, refresh and staleness

The account is organized by the groups the first officer assigned (what changed, what is unresolved, what was only activity), and each item carries its steps, comparison, relations and links, each tied to its evidence. Record IDs are internal references and are never shown as names. Links are labeled and drawn from URLs that appeared in the cited records. How items are laid out, how evidence is displayed and how selection is preserved across a refresh are described in the README and skill; those parts of the renderer were not examined, so they are documented intent here, not observed behavior.

You return to the chat after Hermes has reported more results. The Bridge still shows the account prepared earlier. Pressing Refresh calls the app-only, read-only `refresh_observation_deck` tool directly. It sends no message to the first officer and starts no work. The server rereads the exact chat, treats the panel's copy of the explanation as untrusted input, and validates its citations again against the freshly read records.

If the cited records still exist, the explanation is kept as it was, with its original read time and original fingerprint restored after validation, and the server compares that fingerprint with one computed from the new read. A difference sets an `explanationStale` flag. The panel code in [`view.html`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/view.html) shows the explanation's own source time beside the new read time, labels a stale explanation "older than the records", and offers Update walkthrough. It never restamps the explanation as fresh. If a cited record no longer exists, the refresh fails with a message and the last view is kept. A failed, timed-out, foreign or out-of-order refresh also keeps the last view and reports why; the same holds when the final record in the chat was only partly written. A test in `test_chat_reader.py` checks that a record appended to the chat is visible on reread while existing record IDs do not change, which is what lets an older explanation keep its citations. The only test found for the cross-process hand-off is [`test_process_delivery.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_process_delivery.py), which starts two separate server processes with synthetic records and shows a prepared view surviving into a refresh that is then marked older.

The Bridge adds no monitoring. Records change in the view only when someone refreshes, and a stale flag means the records differ from what the account was built on. It does not say whether the account is still right.

On the Claude side the same staleness idea exists at the record level. The reader produces a fingerprint made of the record count and the last main-session record, and its test suite checks that references stay stable when records are appended or a partial final line later completes. It skips an incomplete last line with a `pending-tail` warning, because Claude Code may still be writing.

## Update: as documented and as measured

Update walkthrough is the one action that asks for new interpretation. The README describes a five-step lifecycle: capture, dispatch, deliver, settle and cancel. The server captures a request by rereading the chat and producing a random request identity, the chat id and a snapshot of fingerprint, read time, record count, a digest of the captured records and a seal over those fields. The panel sends one message asking the first officer to start exactly one read-only subagent without waiting, polling or pausing project work. That subagent reads only the named chat, runs no project task, never retries, and calls `deliver_walkthrough_update` once, either with the request unchanged or with a failure.

The README then records a measured limit that governs what you would see in Codex. In the Codex host measured, the preparation agent could deliver to the server after the parent turn finished, but the originating panel never rendered it. The panel therefore treats the background route as supported only when the host advertises `hostCapabilities.backgroundWalkthroughDelivery`, and the README says the measured host did not. This is a recorded measurement, not proof about whichever Codex you have installed now. The code in `view.html` matches the gate: without that capability, pressing Update shows that background updates are not supported, captures no snapshot, dispatches nothing and starts no timer, and your current view is kept. In the example, the Captain reads that notice and asks the first officer in the chat for a fresh walkthrough through the ordinary open-and-present path.

The full lifecycle is implemented and tested with synthetic records. The sources examined do not show it completing in a live host, and the README says the host-dependent parts need installed-host acceptance that synthetic tests cannot give.

## Cancelling an update

Where the lifecycle does run, cancellation belongs to the preparation request and nothing else. The panel owns the single active request. Cancel update ends it immediately in the panel, then sends one message asking the first officer to interrupt the subagent and discard any result. The ten-minute timeout does the same: it invalidates the request first, then asks for the interruption, and the panel says the stop was requested, not confirmed. If the host cannot be told, the panel says that and still ignores any late result. A late failure of an older request cannot clear a newer one. A substantive new instruction from you supersedes the request unless you say to keep it, and the server, which keeps no state, cannot itself stop a subagent or notice your instruction. The first officer does both.

This is not a general way to cancel Hermes or any worker. Nothing here reaches a Hermes task, the board or a pull request.

## Failures that protect identity and privacy

The delivery step is built to refuse the wrong chat. The reader picks a chat only by the exact id, never by recency, directory or title. In [`preparation.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/preparation.py) and its [tests](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_preparation.py), a delivery is rejected if its request names another chat, its identity or snapshot has been edited, a captured record has changed, the chat now has fewer records than were captured, or the walkthrough cites a record that arrived after the snapshot. Records appended afterwards are allowed; they only make the result older. The seal is, in the source's own words, "a consistency check, not authentication": a caller who can read the chat could build a consistent snapshot, and the seal detects changed text rather than preserving it.

On privacy, the sources agree on several limits. Reasoning is excluded; the Claude reader drops thinking blocks, and its test checks that a marker string in them is absent. Nothing is written: no transcript export, no standalone viewer file. Source text is treated as data, not instructions. Large Claude tool results are shortened to a head and tail excerpt, while your prompts and the assistant's text are kept whole, and the reader warns when that exceeds the size budget. A Claude session that cannot be found, matches twice, belongs to another session, has an unsupported Claude Code version (the range in source is 2.0.0 up to but not including 2.2.0) or contains an unrecognized core record produces a named error, never an empty success.

The records you select are input to the configured explanation process, with reasoning excluded. The sources describe no blanket secret redaction and no guarantee about any model provider, so the boundary to rely on is the one above: what is read, what is left out and where nothing is written. Whether that process is permitted for a given chat is governed by whatever authority you have configured; the identities Blueprint covers what counts as a control and what is only a consent record.

## Show Me and Retro

The panel offers two further buttons. Show Me asks for a visual explanation of the chat or a selected piece of work. Retro asks for suggestions for improving future sessions. Each is optional, needs a separately installed skill, and sends one message to the originating chat only when you click. The message carries the exact thread id, tells the skill to stop on a mismatch, and says not to implement changes or resume project work. A second click is ignored while one is pending. If the skill is missing the chat says so and stops. The panel's own wording is that delivery means Codex accepted the message, not that the skill started or finished. Retro only proposes. A walkthrough excerpt sent with a selected item is reference data to verify against the original records.

Neither button runs your project, merges anything or shows that work was delivered. The issue-delivery Blueprint describes the review and merge record, and you or the first officer still judge it.

## Where this breaks

**Wrong chat.** A wrong identifier that exists in the index would be read without complaint, because the server cannot see the caller's environment. The skill and tool descriptions guard against this by instruction, and the reader guards against guessing, but no code compares the identifier with the chat actually invoking the tool.

**Format drift.** Codex's saved format is private and version-dependent. The adapter rejects what it does not recognize, so a Codex update is likelier to produce an explicit refusal than a wrong answer, but the Bridge is unavailable until the adapter catches up.

**Thin or truncated evidence.** A record cut at 16,000 characters, a Hermes worker whose events are not connected, or an operation without a timestamp each limit what can be said. The honest account either narrows the claim or states the gap on the affected item. The contract cannot require that, so it rests on the first officer following the skill.

**A plausible but wrong account.** Because citations are checked for membership and not meaning, a reader who relies on the panel without opening a cited record is trusting the first officer's interpretation. That is the same trust the Captain already places in the first officer under delegation, now with a trail of record IDs that makes checking possible.

**Narrow host support.** The panel's background Update path depends on a host capability that, according to the README, the measured Codex host did not advertise, and the Claude pane's Update and cancellation behavior is outside what this document establishes.

## Evidence scope

All links pin `MachineWisdomAI/hermes-helmet` at revision `b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c`. The sources were read statically; neither the code nor the tests were run, and no real chat, Bridge panel or host was used. The example chat is hypothetical. Statements about installed hosts come from the README's recorded measurement, not from fresh observation.

- **Documented intent:** [observe-chat/SKILL.md](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/skills/observe-chat/SKILL.md), [captains-bridge/README.md](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/README.md), [identities and delegated authority](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/docs/blueprints/identities-and-delegated-authority.md), [issue delivery and repair](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/docs/blueprints/issue-delivery-and-repair.md).
- **Observed implementation:** [chat_reader.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/chat_reader.py), [saved_chat.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/saved_chat.py), [walkthrough.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/walkthrough.py), [walkthrough_contract.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/walkthrough_contract.py), [server.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/server.py), [preparation.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/preparation.py), [claude_bridge.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/src/hermes_helmet/claude_bridge.py), and the stale-label and capability-gate lines of [view.html](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/view.html).
- **Static test coverage inspected:** [test_chat_reader.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_chat_reader.py), [test_walkthrough_contract.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_walkthrough_contract.py), [test_preparation.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_preparation.py), [test_process_delivery.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/test_process_delivery.py), [test_claude_bridge_read.py](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/tests/test_claude_bridge_read.py), and the shipped [example-walkthrough.json](https://github.com/MachineWisdomAI/hermes-helmet/blob/b0daeb43c183c8941bdbb18ab6a99bb3bfc8075c/mcp/captains-bridge/example-walkthrough.json).
- **Unexercised:** the renderer's layout, evidence display and selection behavior, the Claude pane, and any installed host behavior beyond what the README reports.

If you want to check any claim here, the pinned links above open the exact files, and the cited records in your own walkthrough are the place to compare an account against its evidence.

Hermes Helmet is by [Machine Wisdom AI](https://machine-wisdom.ai/).
