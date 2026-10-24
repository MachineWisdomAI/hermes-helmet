# Hermes Helmet architecture overview

*A retrospective account of Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/), written after the software. It describes what exists, not a proposal, and it does not claim the design was approved before or after it was built. Source and evidence limits are stated once, at the end.*

## What you hand over, and what you keep

If you already work with a coding agent, you probably know the cost of a larger plan. The agent can write the code, but you end up carrying the work between steps: assigning the next task, reading the pull request, relaying review comments, deciding whether it is safe to merge. Hermes Helmet is an open-source software factory for removing that relaying. You agree on a written plan with your coding agent, record it as GitHub issues, and delegate the authority to carry it through implementation, review, repair, and authorized merge. The worker doing the implementation acts under its own GitHub identity, so a reader of any pull request can tell who did what.

Three roles come up throughout, and the product keeps them apart.

- The **Captain** is you. You decide the outcome, the scope, and whether merging is authorized, and you remain the source of authority. Your GitHub account is the `captain_github_login` in the authority policy.
- The **first officer** is your coding agent after you delegate. It runs the bundled skills (`helmet-issue` for one issue, `helmet-epic` for a graph of issues) inside a host such as Codex or Claude Code, using your operating-system account and credentials. It coordinates assignments, reviews each change from a checkout separate from the worker's, directs repairs, and merges when authorized. It is not a second human who approves every step, and it is not a separate GitHub identity.
- The **Hermes worker** is Hermes, the Nous Research agent, running in Docker with its own GitHub account and credentials (`worker_github_login`). It implements, tests, opens pull requests, and repairs the same pull request when review finds a problem. It never merges and never force-pushes.

A few other terms follow from these. A **task** (also called a card) is a native unit of work on the Hermes Kanban board, and an **attachment** is a file stored on a card. A task is not always created from a GitHub issue: in the issue-delivery flow the system creates a root task for each dispatched issue and a repair task when review asks for changes, while tasks in a coordinated multi-part writing flow, including each contribution and the editor's assembly, have no issue of their own. An **issue** is a GitHub issue that records accepted work. An **epic** is a parent issue whose child issues each go through the single-issue flow. A **pull request** (PR) is the worker's proposed change. The **runtime** is the worker's side of the system: the container, its persisted state, and the configured provider and model. The Captain and worker accounts must differ, and the system checks this from both sides.

## Who is responsible for what

The [Captain, first officer, and crew guide](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/captain-and-crew.md) lays out the arrangement as roles, and the [identities and delegated authority Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/identities-and-delegated-authority.md) holds the full contract. Four questions are worth separating.

**Whose credentials?** GitHub records the first officer's reviews and merges under the Captain login, and the worker's commits and pull requests under the worker login. The Captain host holds only the Captain's `gh` profile; the worker credential belongs in the worker runtime. Captain-side preflight refuses to continue if the worker's login is stored on the Captain host, and it refuses a pull request that the configured worker did not author. Neither side switches profiles, tokens, or sessions to act as the other.

**Whose authority?** One version 2 JSON policy names both logins, the repositories the worker may touch, the labels that mark an issue as specified or dispatched, the merge mode, and the limits on elapsed time and repair rounds. Merge defaults to `unattended_when_clean`: the first officer may merge after reviewing the exact head, with required checks passing. An issue or epic can narrow that with a whole-line `Merge when clean: no`, which holds the merge for your separate approval, and an installation set to explicit Captain approval cannot be widened by a marker or a per-run choice.

**Which model?** The worker's provider and model are chosen on the worker, inside its runtime, and are independent of the first officer's host and model, which you choose in that agent's own tooling. The [worker runtime and model choice Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/worker-runtime-and-model-choice.md) and the [model lanes document](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/model-lanes.md) describe the split. Moving the worker to another provider keeps its GitHub account, leaves the repository policy in force, and leaves merge authority with the first officer. That independence is the documented design; no source examined tests it as a changed-model run. One policy field, `captain_model_provider_consent`, records your standing consent for in-scope task and review content to go to the configured worker provider. It is an instruction to Captain skills and not a runtime check that stops content from leaving.

**Where does it run?** The `helmet` command and the first-officer skills install on the Captain host from the source checkout. The worker comes from a published container image that Docker Compose pulls, with its state in a named volume. These are separate artifacts, and a source checkout says nothing about which image a given container is running. Optional integrations are off in the minimum runtime, and declining all of them leaves the delivery loop available; they are described near the end of this overview.

## A representative project

The rest of this overview follows one invented project. It is hypothetical: nothing below was implemented or run, and it exists only to show how the pieces connect.

Suppose you want a new service endpoint, say a read-only status summary for a small API, delivered with its code, its tests, and explanatory documentation. You and your coding agent shape the deliverable and record it as a parent issue with two children. The first child is the endpoint with its tests. The second is the documentation that explains it, and it declares the first as a blocker. You accept the plan and state the outcome, the scope, and whether merge is authorized. Writing the issues authorizes nothing; your acceptance does.

### Dependencies and dispatch

Your agent, now the first officer, starts the work with `helmet-epic`. The epic Blueprint, [epic coordination](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/epic-coordination.md), describes how the graph is read from GitHub on every pass, from native sub-issues or from `Parent` lines, with dependencies taken from `Blocked by` sections and GitHub's native dependency list. The graph gets a fingerprint, and you accept it before anything dispatches. If the plan is later rewired while work is in flight, the epic pauses until you accept the refreshed graph.

In the example, the endpoint issue is ready and the documentation issue is blocked, so only the first is handed to the single-issue flow. The epic also has a parallelism limit, two by default, that reflects configured capacity and assumes nothing about who is available. The parent epic is never itself dispatched.

The single-issue flow in the [issue delivery and repair Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/issue-delivery-and-repair.md) starts with preflight: it checks the Captain identity, the policy, and that the issue is open and carries the ready or dispatch label. It looks for existing progress before creating anything, and the issue poller creates exactly one task for the issue. Resuming adopts that task rather than creating a second.

### Worker implementation

The worker takes the task, works in its assigned worktree, opens a tested pull request under its own account, and records the pull request as `metadata.published_pr`. The documentation describes this sequence as intent; the code and tests examined establish the task instructions and completion contract but do not observe what a worker does once it starts.

### Independent review of the exact head

The first officer then reviews the exact head of the pull request as the Captain. The independence comes from the identities: the author is the worker account, the reviewer is the Captain account, and the review happens in a separate checkout. A review belongs to a commit. Only formal GitHub reviews from the Captain login on the current head count, and recording a clean result requires a matching `APPROVED` review on that commit.

### Same-PR correction

Suppose review of the first head finds a consequential defect in the endpoint, such as a missing error case. The first officer posts a formal changes-requested review on GitHub and records the outcome. It does not create a repair task or push to the worker's branch. The issue poller, on its next cycle, reads trusted review activity on the pull request and creates one dependent repair task assigned to the same role as the original. Hermes reads the review on GitHub and repairs the existing pull request, producing a second head. That head is reviewed afresh, and any earlier clean result no longer applies. Each distinct changes-requested review spends one repair round up to the policy limit. A clean pull request needs no invented repair, and a failing check alone does not start one.

### Authorized merge and acceptance

When the current head has a clean Captain review, the first officer runs `helmet merge ISSUE_URL --expected-head SHA`. The merge gate re-reads the pull request and withholds the merge if the head has moved, a repair is pending, the pull request is a draft or unmergeable, or required checks are not verified green. It treats GitHub's `clean` state as insufficient on its own, and under the `local-only` worker completion contract with no configured checks it falls back to explicit approval. If the merge is allowed, one request carries the head SHA so GitHub itself declines if the branch moves, and the result is confirmed afterward. The worker never merges.

For an issue implemented in its own repository, merging completes the work. A tracking issue routed to a separate implementation repository works differently: the merge moves it to awaiting acceptance, and `helmet accept ISSUE_URL --expected-head SHA` closes it only if the merged head matches the clean-reviewed head. Closing the parent epic is left to you by default; this is a default and not a gate on the children.

Merge and acceptance are not release or publication. The Blueprints treat code readiness, runtime acceptance, and publication authority as separate claims, and the files examined contain no release or publish command. Merging the endpoint does not mean it has shipped.

## The explanatory document

When the endpoint merges, the documentation child is no longer blocked and becomes ready. The code travelled through GitHub issues, pull requests and review. The document that explains it can travel a different way, because it has several parts and so is not simply handed to a builder to write and integrate.

### Coordinated writing

Suppose the document has two parts, one for people who call the endpoint and one for people who run it. The [coordinated writing Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/coordinated-writing.md) describes this case: writing that splits into parts, each written by a Hermes worker, then assembled by a separate editor. A short edit or a review-only request does not use it, and nothing makes coordinated writing a required workflow.

The first officer writes one plan naming the audience, purpose, format, sources and a brief for each part. Each writer receives only its own sources and the neighboring passages it needs. The plan creates a card per part and an editor card that stays held. A writer finishes by requesting review through the native call, which attaches its file and routes the card to the first officer.

Every submission is named by a revision token that includes the attachment id and the file's SHA-256. The first officer downloads that exact file, reads it against the sources, and either requests changes or accepts. Accepting records any editorial decisions the editor must honor, and those recorded decisions are the only channel from review to editor. The editor is released only by a command that checks each part's accepted revision, and it receives the accepted files, not the drafts that were sent back. After the editor's own submission is accepted, `helmet write canonical` retrieves the accepted file as the final document.

The reviewer's identity is checked against the Hermes runtime that executes the command, so a writer cannot accept its own card. The separation of duties beyond that, such as writers never creating cards or touching one another's files, is mostly written instruction to the agents. The coordinated writing Blueprint labels each rule as code or instruction, and this overview does not upgrade any of them.

### Where the bytes travel

Writers and the first officer do not share a folder. The [task artifacts Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/task-artifacts.md) describes the layer underneath. The first officer's side validates names, sizes and checksums and decides whether a retrieval may succeed. An adapter owns everything across the boundary to the worker: authentication, which identity writes, and the native attachment calls. Hermes Helmet ships one reference adapter, for a worker in a local Docker Compose project, and a deployment with another topology writes its own to the same three verbs.

An artifact is named by a reference containing the board, task, native attachment id and checksum. Retrieval returns the exact bytes the reference names or an error, never a newer file, and a changed file is a new attachment with a new reference. If the downloaded bytes do not match the listing, nothing is kept.

The two Blueprints meet at one point, and their wording needs reconciling. The task artifacts Blueprint says retrieval integrity answers only whether the exact bytes arrived intact, and that its sources define no acceptance state for an artifact. The coordinated writing Blueprint supplies the acceptance: the first officer's judgment, recorded in the card's run metadata against one revision. For the endpoint document, a successful download tells the first officer it holds revision X, the first officer decides whether X is good, and acceptance names X. The artifact layer adds no acceptance of its own.

### From accepted text to the repository

Retrieval ends with bytes written to a path. The coordinated writing Blueprint describes nothing past that point, and acceptance of the text is not integration or publication. For the endpoint, the document still has to reach the repository that owns it, and that happens through the ordinary code-delivery path described above. The first officer hands the accepted exact artifact to a Hermes worker as the input for an integration task. That worker, under its own GitHub account, authors a pull request in the owning repository. The first officer then reviews the exact head of that pull request, independently of the worker, and applies merge or acceptance only within the authority you delegated. A worker never merges. If the integration worker edits the text or the branch moves after review, the earlier review does not carry over; the identities Blueprint describes the head checks that withhold a merge in that case.

This overview presents the handoff as the existing delivery path applied to a document, not as a separate writing feature, and the pinned sources do not show that exact sequence run for a document.

## Asking what the chat came to

During a long delegation, you may return to a chat and want to know what the work amounted to. The [Captain's Bridge Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/captains-bridge.md) covers that path. The first officer reads the saved records of one chat, writes an account of the work that cites those records, and the Bridge shows the account in a panel.

For the endpoint, the chat might contain the request for the endpoint, the handoff to Hermes, the worker's report with a pull request link, and the first officer's note that the document has been accepted. The Bridge keeps three kinds of statement apart. Recorded facts are the saved records with their identifiers. Generated interpretation is the walkthrough the first officer writes from them. Acceptance or delivery, which depends on human or source judgment, is something the Bridge does not decide: it never looks at the pull request, runs tests, or checks that a merge happened. If the chat records only a worker's report, the account says "Reported complete", not "Merged".

What the code checks is that every citation names a record from this chat as it was read, that the shape and sizes are within limits, and that a quoted URL appears in cited text. It does not check that a cited record says what the account claims. A plausible but wrong account is therefore possible, and the remedy is the one the Blueprint states: open the cited record and compare. You already trust the first officer's interpretation under delegation, and the Bridge adds a trail of record identifiers that makes checking possible. When more records arrive, Refresh rereads the chat and marks the earlier account as older than the records. It does not restamp the account as fresh, and it does not say whether the account is still right.

The Bridge's reach is narrow in ways that matter for this flow. The reader sees Codex's saved turns, and the Bridge does not collect worker-side Hermes events, so worker results appear only where the chat itself recorded them. Reasoning records are excluded, nothing is written to disk, and the Blueprint finds no blanket secret redaction and no guarantee about a model provider. The records selected are input to whatever explanation process you have configured, and whether that is permitted is a matter of your delegated authority; as noted above, the consent field is a record and not a runtime gate. The Blueprint also says the Codex panel and the Claude Code textual path are not feature-equal, and it documents the background Update action only for Codex, where the measured host did not render it.

## Time, interruptions, and attention

Each issue has an elapsed-time window and each epic has its own, both measured from the original start. The tested default window is 8,640 minutes, six days. They are elapsed windows, not a promise of continuous execution or a spending cap. Resuming a session resets neither, and a resumed pass whose budget is spent stops before changing any label or task.

The first officer's chat session is not the unit of continuity. Checkpoints on the Captain host and GitHub's own records let a later first officer pick the work back up, adopting an existing root task instead of creating another. Something has to wake the first officer, and that belongs to the host: either a background task that delivers its completion back, or one native schedule, five minutes by default, with each wake reconciling once and staying quiet when nothing changed. If the host offers neither, the first officer runs one truthful pass and says what is missing.

The epic's status report groups children into six buckets: `completed`, `active`, `ready`, `blocked`, `failed`, and `awaiting_human`. These are names for reporting. Despite its name, `awaiting_human` means a child is waiting on an action the automatic path will not take on its own, such as accepting a routed child, and the first officer acting under your delegation can perform some of those. Only an explicit human-only marker or a policy restriction asks for you. You can check status at any point, but you are not required to visit any bucket to keep the work moving. Nothing in these flows makes a human step mandatory between a clean exact-head or exact-revision review and release.

## Inspecting and correcting

The record is on GitHub: issues, reviews, heads, merges, and the worker's own commits under its own login. The first officer keeps a small secret-free checkpoint on your host, and `helmet status`, `helmet epic-status`, and a read-only `helmet wait` show where things stand. Formal issue and pull request review prose stays on GitHub and is not copied into the task board. Coordinated-writing request-changes feedback is different: it is recorded as native card comments, as the writing Blueprint describes. Each record stays in its owning system. You can also read any earlier writing revision and the Bridge account for a chat.

Corrections follow the same path as the work. For code, changes requested on the pull request lead to a repair on that pull request. For writing, the first officer requests changes, the card returns to the same writing profile with the feedback attached, and `escalation_due` appears after two requested rounds. Changing approach after that is the first officer's decision, backed by a counter. To replace an accepted part, `helmet write supersede` creates a new card and holds the editor again, but it refuses once the editor is running or done, and it does not cancel the old card.

If repeated repairs show a wrong approach, an authorized first officer can preserve the old work, stop the old attempt, supersede it with a distinct successor issue and a smaller skeleton, and have you accept the refreshed graph. Stopping is limited: closing a pull request or recording a blocker does not cancel a running worker, the portable interface has no cancellation verb, and a successor stays undispatched until the old attempt cannot write. When the first officer's authorized controls cannot hold the old execution, the review guidance says to report that blocker and not to claim isolation.

Worker identity holds across all of this. The writing role is a routing choice: the identities Blueprint states that every role profile gets the same worker GitHub identity and that routing changes assignment, never authority. The document's writers and the integration worker are Hermes under its own account, and the first officer accepts their work under yours.

## Optional company configuration and governed records

Two optional things are easy to confuse. A company overlay is a pattern for who owns deployment configuration. FAVA Trails is an optional integration with its own enablement. Neither is a service you switch on in the same way, and the flows above depend on neither. OpenViking and Signal are further optional integrations that stay off in the minimum runtime.

The [company deployment overlay](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/private-overlay.md) is how a team keeps its authority policy and deployment configuration in its own repository while running the published worker image. The image owns the Hermes Helmet runtime. The company owns the policy, the repository allowlist, the two GitHub identities, and the worker and provider credentials, which stay out of Git. The document tells adopters to depend on the published image by digest and not to fork the core. Its examples use a fictional company, and Machine Wisdom's own deployment is not something an adopter needs to install or copy. The public reference deployment in the Hermes Helmet repository can be used without a company-private overlay repository. The document names OpenViking, FAVA Trails and extra model lanes as separate optional pieces that stay off until their own guides say to enable them, so the overlay does not imply any of them.

[FAVA Trails](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/fava-trails.md) is the optional integration for a governed record: decisions, observations, validation and lineage. Hermes Helmet starts and operates when it is declined or unavailable. The public Compose stack does not run it, and the integration is enabled only by a policy setting after the principals and the company data repository are ready, so it is a deliberate adoption step and not a default. The first officer and the worker are separate ordinary principals with their own process identities, and neither holds operator powers. OpenViking, where present, is working context across clients, and moving something from it into FAVA Trails is an explicit decision that leaves a governed trail, not automatic copying. Hermes Helmet defers to FAVA Trails's own contracts rather than reimplementing governance.

For the endpoint, a team that has enabled FAVA Trails could record a design decision there. The pinned documents describe no automatic path from an accepted writing artifact or a Bridge account into FAVA Trails, and none is claimed here. The documented lifecycle example uses injected trust verdicts and does not claim live tool discovery, private adopter acceptance or automatic ingestion.

## Where it breaks

The checks mostly fail closed: the run stops, saves a coded blocker, and does not guess. Examples are a wrong login, a worker credential found on the Captain host, a pull request the worker did not author, or a pull request with no recoverable task. Several limits sit outside that pattern. The merge gate protects merges made through `helmet merge`, and nothing examined stops someone holding merge credentials from merging outside it. The consent field is a record and not an egress gate. Provider failover belongs to Hermes Agent, and the sources show no exercised failover. A head that moves after review is refused at each step that would use the stale approval, as the identities Blueprint traces, but the sources describe that sequence rather than show it running. A plausible but wrong Bridge account is possible because citations are checked for existence and not for meaning. Most writing separation-of-duties rules are instructions to agents, not code.

## The seven Blueprints

This overview keeps detailed contracts in their owners. Read these for the rules themselves:

1. [Identities and delegated authority](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/identities-and-delegated-authority.md)
2. [Worker runtime and model choice](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/worker-runtime-and-model-choice.md)
3. [Epic coordination](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/epic-coordination.md)
4. [Issue delivery and repair](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/issue-delivery-and-repair.md)
5. [Coordinated writing](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/coordinated-writing.md)
6. [Task artifacts](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/task-artifacts.md)
7. [The Captain's Bridge](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/blueprints/captains-bridge.md)

Supporting documents: the [Captain, first officer, and crew guide](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/captain-and-crew.md), the [model lanes document](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/model-lanes.md), the [company deployment overlay](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/private-overlay.md) and [FAVA Trails](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/fava-trails.md).

## Evidence scope

This overview is a static reading of the public documentation, source and tests at `MachineWisdomAI/hermes-helmet` revision [`1111ae9`](https://github.com/MachineWisdomAI/hermes-helmet/tree/1111ae97df61a78cb661cff1a8bd52431566cc32). Each Blueprint pins its own claims to an earlier source revision: task artifacts at `2188219`, coordinated writing at `dff3741`, the Bridge at `b0daeb4` and identities at `ec7e238`. Where this overview compresses a Blueprint, it follows the Blueprint's owning text and inherits its scope and limits. The example project is hypothetical and was not implemented or run. No code, test, board, chat, panel or deployment was run for this description, and the end-to-end flow was assembled from separate behaviors rather than observed as one run. The documentation reports that the first-officer workflow has been exercised through Codex, while Claude Code and Hermes have installation and static validation only; this reading does not confirm it. A merged source revision is not evidence of any model, runtime, or deployment.

If you want to see this on your own plan, start with the [quickstart](https://github.com/MachineWisdomAI/hermes-helmet/blob/1111ae97df61a78cb661cff1a8bd52431566cc32/docs/quickstart.md), write a small set of issues, and send them through Hermes Helmet.
