# Epic coordination in Hermes Helmet

*Hermes Helmet by [Machine Wisdom AI](https://machine-wisdom.ai/). This document describes existing epic orchestration as a retrospective Blueprint, written after the code. It does not claim that the design was approved before it was built. Evidence is pinned at revision `22592c07198bfe4843b60249cadd1135b43b0ee1`; the [evidence scope](#evidence-scope) at the end says what was and was not checked.*

An epic in Hermes Helmet is a parent GitHub issue whose child issues each go through the single-issue flow described in the [issue delivery and repair Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/blueprints/issue-delivery-and-repair.md). The actors and their separate identities are described in the [identities and delegated authority Blueprint](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/blueprints/identities-and-delegated-authority.md) and in [Captain, first officer, and crew](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/captain-and-crew.md). This document relies on those and does not repeat them.

What sits between them is the subject here: how the first officer learns which issues belong to the epic, which of them may start, how many run at once, what happens when one fails, and how the epic continues when a session ends. The first officer is your coding agent running the bundled `helmet-epic` skill under your account. You are the Captain.

Function names below are in [`helmet_epic.py`](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/src/hermes_helmet/helmet_epic.py), and behavior is taken from that source and its [tests](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/tests/test_helmet_epic.py). Statements of intent come from the [epic guide](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/helmet-epic.md) and the [`helmet-epic` skill](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/skills/helmet-epic/SKILL.md), and are marked as such.

## The example epic

One hypothetical epic runs through this document. Nothing here is a recorded run.

Five children sit under one open parent issue:

- **A** adds a data model. Nothing blocks it.
- **B** builds an API on top of A, so it declares A as a blocker.
- **C** updates command-line help. Nothing blocks it.
- **D** is the public announcement. Its title marks it human only.
- **E** is a tracking issue routed to a second repository, where its code will live.

The parallelism limit is the default of 2.

## Where the graph comes from

The first officer runs `helmet epic EPIC_URL` for one pass, or `helmet epic-status EPIC_URL` to see the buckets without dispatching anything. Both read the graph from GitHub on every pass. The documentation says explicitly that parent relationships, dependencies and membership authorization are revalidated live, and that a previously accepted graph is not cached as current truth.

The graph answers two separate questions, from different sources.

**Membership** asks which issues are children of this epic. `build_epic_graph` first asks GitHub's native sub-issue list for the root, through `_try_native_sub_issues`. If that list has members, it is authoritative and the loader does not search body links. If the endpoint answers 404, or answers with an empty list, the loader falls back to `_search_parent_children`, which searches the epic's own repository for issues whose body names the epic as `Parent`. An epic with no native sub-issues can therefore still be assembled from body links in its own repository. A 401, 403 or other denied or malformed answer is different: it raises an error and the pass stops, so a permissions failure cannot quietly send the loader down the fallback path. The documentation describes the fallback as three queries regardless of how many repositories are enrolled. Any child tracking issue that lives in another repository has to be a native sub-issue or be passed with `--child ISSUE_URL` on each pass. Candidates found by search are filtered through `_filter_search_candidates_by_parent`, so a body that merely mentions the epic does not join it.

**Dependencies** ask which children must finish first. The loader takes the union of two sources, deduplicated: the `Blocked by` section of the child's body, parsed by `parse_blocked_by_links`, and the child's native `dependencies/blocked_by` list, read by `_try_native_blocked_by`. Native dependencies add to the body's edges and do not replace them. The docstring of `_try_native_sub_issues` states that dependency endpoints are not consulted for membership, and `test_native_dependency_not_used_as_membership` covers it. A blocker is a prerequisite only; it never makes an issue a child.

In the example epic, B's body names A under `Blocked by`. If the epic's children are native sub-issues, they are all members through GitHub's list. If the epic has none, A through E are found by their `Parent` lines, provided each tracking issue lives in the epic's own repository. E's tracking issue lives there too; only its code is routed to a second repository, so body discovery finds E normally and the implementation repository plays no part in membership. Only a tracking issue that itself lives in another repository would need to be a native sub-issue or be passed with `--child`.

Two rules limit what counts as a link. `parse_parent_links` and `parse_blocked_by_links` accept full issue URLs or same-repository `#N` references, and only inside operative `Parent` and `Blocked by` sections or lines. The documentation says edges are never inferred from table row order or narrative prose such as "depends on". A malformed link raises an error and is not skipped. When a child is native, GitHub's answer wins. For a child admitted through the body-link fallback or `--child`, `native_parent_of` is asked for its live native parent. If one exists and is a different issue, the load fails with `child_native_parent_not_epic`, so a stale body line cannot keep a child that has moved elsewhere. Only if no native parent exists does the body `Parent` line decide, and it must name this epic and only this epic. The guide's [graph model](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/helmet-epic.md#graph-model) lists the discovery rules in full.

## What validation rejects

After loading, `build_epic_graph` refuses to return a graph it cannot reason about. The failures visible in the source are: an epic with no children, the root listed as a child, a duplicate `--child` seed, a child with a missing or ambiguous `Parent`, an issue blocked by itself or by the epic root, duplicate blocker links, a blocker that cannot be loaded or is outside the allowlist and enrollment scope, a blocker closed as `not_planned` or `duplicate`, and a cycle among the children (`_assert_acyclic`). Blockers that are not children of the epic are allowed. They are loaded as external blockers and their state counts toward readiness.

Reading the native relationship endpoints also fails closed. `_is_native_relationship_unavailable` treats only a 404 as "this relationship does not exist here." A 401 or 403 stops the pass; `test_native_blocked_by_http_403_fails_closed` covers the blocked-by case. A permissions failure should not quietly shrink the graph and let more work look ready.

## Accepting the graph

A valid graph gets a fingerprint. `graph_fingerprint` hashes the root URL, the sorted child URLs and each child's sorted blocker URLs with SHA-256. Titles, bodies and labels are not part of it, so editing prose does not disturb an accepted plan. Adding a child, removing one, or changing a dependency does.

Before dispatch, the Captain accepts the graph by running `helmet epic EPIC_URL --config policy.json --accept-graph`. The checkpoint stores both the current `graph_fingerprint` and the `accepted_fingerprint`. In `run_epic_pass`, a checkpoint with no accepted fingerprint adopts the new one when the operator passes `--accept-graph` or the checkpoint is still in its initial `PREFLIGHT` state. If an accepted fingerprint exists and no longer matches, the state becomes `GRAPH_CHANGED`, the pass records the blocker, builds a status report and returns before any child is invoked. Running again with `--accept-graph` accepts the refreshed graph and continues. `test_graph_change_pauses_dispatch` and `test_scoped_membership_change_requires_graph_acceptance` exercise this. The effect is that the Captain's agreed plan cannot grow or rewire while work is in flight without someone saying so.

The first officer then reports the effective budget windows and the host continuation it registered.

## Classifying children and choosing the frontier

With an accepted graph, `classify_children` sorts every child into one of six buckets, which `helmet epic-status` prints:

| Bucket | What lands there |
| --- | --- |
| completed | Closed as complete, or the child's own run reached `DONE` |
| active | A child run is in flight: waiting for a pull request, review, repair or merge, with the unattended merge gate counting as active progress |
| ready | Open, every blocker satisfied, not yet started |
| blocked | Open, with at least one blocker that is not complete |
| failed | The child's run reached `FAILED`, or the issue is in an invalid closed state |
| awaiting_human | A human-only child, a review-only enrolled child, a run that is terminally blocked, a routed child whose code merged but whose tracking issue is not yet accepted, or a merge held for explicit Captain approval |

The order of checks matters. Children already closed as completed go first. A child marked human-only (`is_human_only_issue`, which requires an operative marker in the title or body rather than a passing mention) or enrolled for review only goes to awaiting_human and is never dispatched. A child whose blockers are not all complete (`blockers_satisfied`) is blocked. Everything left is checked with the single-issue status for that child: a finished run counts as completed, a failed run as failed, a run stopped at a human gate as awaiting_human, a run in progress as active, and a child with no run at all as ready.

`awaiting_human` is the name of the bucket. It means the child is waiting on an action the automatic path will not take by itself. It does not mean a person must be present: the first officer, acting under the Captain's delegation, can perform some of those actions, such as accepting a routed child. The guide and the skill describe this bucket in slightly different words. The classification code covers all of the cases above, and this document follows the code.

Recovery lives in the last classification step. If the single-issue status reports an existing root task for the child, the child is active even when its local checkpoint is empty or unknown, and it is never offered as ready. That prevents a second root for work already in flight. `test_recovered_root_counts_toward_parallelism` shows a recovered child counting against the limit, and the skill says the same.

`compute_ready_frontier` is then very small. It takes the active and ready lists, computes `free = max_parallelism - len(active)`, and returns the first `free` ready children, in sorted URL order. If there are failed children and `continue_independent_branches` is false, it returns nothing. By default independent branches continue. `compute_resume_children` returns the active list unchanged. `run_epic_pass` invokes the resume list first and then the frontier, skipping repeats, so work already in flight is re-entered before new work takes a slot. `max_parallelism` comes from the policy's `max_epic_parallelism` (default 2) unless a pass overrides it; preflight rejects a value below 1.

## Three separate gates

Being ready does not mean being allowed to run, and being allowed does not mean there is room. The code keeps these apart.

Readiness is a graph property. It follows from the accepted graph and the live state of blockers, and nothing else.

Authority is policy and identity. `preflight_epic` checks that the Captain and worker identities differ, that the Captain's GitHub login matches the session, that no worker profile exists on the Captain's host, that the ready and dispatch labels differ, and that budgets are valid. Repositories must be allowlisted or covered by an enrollment receipt, and review-only enrollment never dispatches. Merge authority for each child is inherited from the epic body through `merge_authority_for`, so `Merge when clean: no` on the epic narrows its children. The identity Blueprint covers these identities and they are not repeated here.

Capacity is the parallelism limit and the epic's time window. `epic_budget_exhausted` checks the original `started_at` against `max_epic_runtime_minutes` before each child invocation. The skill adds that a default limit of two does not imply two humans or two available workers are free; the first officer is told to use actual capacity.

The parent epic is outside all three. `preflight_epic` raises `epic_root_has_dispatch_label` if the root carries the dispatch label, `_ensure_root_not_dispatched` checks it again at the end of a pass, and `run_epic_pass` raises `refusing_dispatch_epic_root` if the root URL ever reaches the invoker. The epic is a record of the plan and a place for status. It is never itself a unit of work.

## The first pass

On the first pass of the example epic, A, C and E are ready. B is blocked because A is not complete, and D is awaiting a human. Nothing is active, so two slots are free and the frontier is the first two ready children in sorted URL order. Say they are A and C. Each is handed to the single-issue flow, which creates one root task for it. E stays ready until a slot frees, and B stays blocked without using a slot. The limit reflects your configured capacity and assumes nothing about humans being available.

With a limit of 1, the same epic would run one ready child first and the next only after it finished and freed the slot.

On later passes, A and C are active. The frontier is empty because no slots are free, and the pass re-enters A and C so the issue flow can wait on checks, review or repair. If C finishes first, nothing changes for B, because B's edge points at A, but a slot opens and E starts.

## One branch fails

Suppose A's pull request comes back with changes requested, Hermes repairs it, and a later review requests changes again until the repair count passes the policy limit. The single-issue run fails with a budget error, and A moves to **failed**.

B does not start. A failed blocker never completes, so B stays **blocked** even though A is no longer making progress. C is independent, so it continues. This is the default: the epic keeps running unrelated branches while one child is stuck. `test_blocked_branch_continues_independent_by_default` covers the continuing-independent-work part, and the pinned tests check that the ready frontier still returns an independent child when another child has failed and a third is blocked. The pass ends in the `WAITING` state with the failure recorded as the blocker, so the next status report shows the failed child without stopping the rest.

Nothing retries A automatically. Failure is a fact the Captain and first officer have to act on.

## Review, merge and routed acceptance on the surviving branches

C follows the ordinary path. Hermes opens a pull request under its own worker account, the first officer reviews the exact head as the Captain, and the merge goes through `helmet merge ISSUE_URL --expected-head SHA`. Epic-level merge authority is inherited. By default a clean, reviewed head with passing required checks can be merged by the first officer without a separate approval. A whole-line `Merge when clean: no` in the epic body narrows every child to explicit Captain approval, and a child can re-enable autonomy with its own `Merge when clean: yes`. An installation set to explicit approval is a ceiling no child can raise. When C's merge completes, C moves to **completed** and its slot goes to E.

E behaves differently. Its code merges in the implementation repository, but its tracking issue stays open and the child moves to **awaiting_human**, where it waits for acceptance rather than for any particular person. Dependents of E remain blocked on the closure of the tracking issue, not on the merged code. The Captain-side `helmet accept ISSUE_URL --expected-head SHA` command, which the first officer can run under the Captain's delegation, closes the tracking issue only when the merged head matches a head that received a clean Captain review. After that, E is **completed**. The [issue routing guide](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/issue-routing.md) covers routing in detail. Acceptance records that the Captain accepts the merged head for the tracking issue. It does not mean the change has been released or published.

## Replacing a failed child

While A is failed, the Captain decides to replace it. The delivery guidance describes the sequence:

1. Preserve the old pull request, branch, tests and evidence, and prepare a distinct successor issue that states the original outcome and a smaller starting skeleton.
2. Quiesce the old attempt through the deployed controller, and confirm that it cannot write again. Recording `blocked` or closing a pull request does not stop a running worker, and the portable interface has no cancellation verb. If the old execution cannot be stopped safely, the successor stays undispatched and the first officer reports that exact blocker.
3. Close the old pull request as superseded, with a link to the successor. The original objective stays visibly unfinished.
4. Rewire the epic's membership and dependencies to the successor, then explicitly accept the refreshed graph before any new dispatch.

The rewiring has to do two things. The successor, call it A2, joins the epic as a child, and B's blocking edge moves from A to A2. The delivery guidance says "rewire affected epic membership/dependencies" but does not spell out how A leaves the graph. The epic's membership rules and its completion test imply the answer. Completion requires that no child is failed, and a failed A that is still a current child keeps the epic at `FAILED` or `WAITING`, never `DONE`. So the replacement graph the Captain accepts must no longer count A as a current child. A's pull request, branch, checkpoint and ledger history stay in place, and its original objective stays visibly unfinished until A2 delivers it. How A is detached in practice, for instance by unlinking the native sub-issue, is a reading of those rules and not something the sources describe step by step.

The rewiring changes the graph's fingerprint, and that sets the epic to `GRAPH_CHANGED`. In that state the epic still refreshes its status report, but it will not dispatch until the Captain runs the pass again with `--accept-graph`. In the code, the pass saves the checkpoint, builds the report and returns before any child is invoked. In practice that holds back in-flight children as well as new ones until the refreshed graph is accepted, which is stricter than the guide's wording of pausing "new" dispatch. A closed-as-incomplete predecessor never satisfies a dependency, so closing A's old pull request does not release B. Only A2's completion does. A2 runs through the same issue path on a fresh branch, and merge authority is revalidated for it instead of copied from A.

The rest of this example assumes the replacement graph is accepted: A2 replaces A as a current child, B is blocked on A2, and C, D and E are unchanged.

## An interrupted session

Suppose the first officer's chat session ends while A2 and E's acceptance are pending. Nothing in the epic depends on that session staying alive. The epic's checkpoint is a secret-free JSON file under `~/.hermes-helmet/checkpoints/`, with the graph fingerprint, the accepted fingerprint, the bucket lists, the parallelism, the original `started_at` and structured notes. Each child has its own checkpoint, and GitHub remains the source of truth for issues, pull requests, reviews and merge state.

When the Captain reinvokes the epic, the pass loads the checkpoint and reads GitHub again before acting. It re-enters in-flight children, including those waiting on a repair, before it allocates a new ready slot, and a recovered live root counts toward the parallelism limit. The per-issue path adopts an existing root task instead of creating another. The pinned tests cover resuming a repair before new ready work, counting a recovered root, and not duplicating children that are already active.

The clock does not restart. The epic's `started_at` is set once and is the origin of the epic window. Each child's budget window runs from that child's own original start, recorded in its own checkpoint, and is distinct from the epic's start. Resuming the epic resets neither. The pinned tests use windows of 8,640 minutes, six days, and check that a default window is still live after seven hours. Once the epic window has elapsed, the next pass records a failed budget blocker and invokes nothing, while an epic whose children are all complete stays done. Both windows are elapsed time. They do not promise continuous execution or unlimited model spend. The first officer announces the effective values, including any override, from `helmet epic-status --json`.

Something must wake the first officer, and the host owns that. The [host continuation procedure](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/skills/helmet-issue/references/host-continuation.md) selects one of two mechanisms. If the host can run a background task that delivers its completion back to the first officer, each active child gets one read-only `helmet wait` with a saved cursor. Otherwise the epic gets one native schedule that covers the whole frontier, at the policy's check interval of five minutes by default unless the Captain set another. Child passes reuse that schedule instead of creating their own. Each wake reconciles the whole epic once and stays quiet when nothing has changed. After an interruption the first officer adopts a surviving schedule or wait handle before registering a new one, and it does not restart a worker just to restore observation. A restart or a change of cadence keeps the original start and the merge choices. The pinned tests include a scheduled cadence surviving a resume without resetting the start.

Continuation ends when no authorized work remains, when the original budget expires, or when a human action is required with no independent work left. If the host offers neither mechanism, the first officer runs one truthful pass with `--one-pass-only`, says what is missing and stops. It does not promise background resumption it cannot provide.

## Reconciliation and closeout

When A2 merges and passes review, B becomes ready, runs and completes. The last `epic-status` shows A2, B, C and E completed, with no failed, active, ready or blocked children. When every child is complete and none is failed, active, ready or blocked, the epic's state becomes `DONE`. That state ignores human-only children, so D remains in `awaiting_human` because its title declares a human-only gate. The pass does not close the parent issue. This outcome depends on the accepted replacement graph. Had A stayed a current child, the report would show A as failed and the epic would not be `DONE`.

Leaving the parent open is the default. It is the Captain's closeout, and it is a default of the code, not a gate on any child. Children flow through implementation, review and an authorized merge without it. Some actions need Captain authority: a merge held for approval, a routed acceptance, or a review in a review-only repository. An authorized first officer acting under the Captain's delegation may perform those. Only an explicit human-only marker or a policy restriction asks for a person's attention. The first officer checks the requested outcome against the delivered evidence before it reports, because an empty pull request queue is not program completion. It also keeps three claims apart: code readiness, any runtime acceptance the plan requires, and publication authority.

## Who is accountable

The first officer is the coding agent you delegated to, running on your account with the bundled skills. It is your accountable counterpart for this epic in the sense that it coordinates, reviews, directs repairs and merges within the authority you recorded, and it reports against the goal you accepted. It is not a long-lived session. A session can end, and the epic continues from checkpoints and GitHub the next time a first officer picks it up. The role carries no authority beyond your delegation. It stays within the allowlist, honors `Merge when clean: no` and installation ceilings, and cannot use the worker's identity or credentials.

Hermes works under its own account, never merges and never force-pushes. It does not run the epic skill. Switching the first officer's host or Hermes's model changes neither the graph, the checkpoint format nor who holds authority.

## Evidence scope

This document rests on a static reading of the sources below at revision `22592c07198bfe4843b60249cadd1135b43b0ee1`. The example epic is hypothetical, and no run of it was observed. Observed implementation comes from `src/hermes_helmet/helmet_epic.py` and `tests/test_helmet_epic.py`. The tests replace child invocation and the GitHub runner with stand-ins, so they exercise classification, the frontier, graph acceptance, resume and budget checks without live GitHub or a live worker. Documented intent comes from the [epic guide](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/helmet-epic.md), the [`helmet-epic` skill](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/skills/helmet-epic/SKILL.md), [host continuation](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/skills/helmet-issue/references/host-continuation.md) and the [review and recovery guidance](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/skills/helmet-issue/references/delivery.md).

The guide states that the preview's runtime workflow has been exercised through Codex, and that installation and static validation cover Codex, Claude Code and Hermes targets. Neither claim was confirmed here, and the code was not run. No test was found that carries a five-child graph through consecutive real passes, nor one that carries a failed child through supersession, graph rewiring, removal of the failed child from membership, acceptance and the successor's completion as one sequence, nor one that runs a scheduled wake against a live host. The walkthrough is assembled from function behavior, the tests named above and the documented procedure. Scheduled continuation and cross-repository epics depend on host and enrollment details outside this document, and are described only where the source states the rule.

If you want to see this loop on your own plan, write a few issues, link them the way the example does, and run `helmet epic-status` before you let anything dispatch. The [quickstart](https://github.com/MachineWisdomAI/hermes-helmet/blob/22592c07198bfe4843b60249cadd1135b43b0ee1/docs/quickstart.md) walks through writing a policy and sending a first issue through Hermes Helmet.
