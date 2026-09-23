# Review and recovery decisions

Use this guidance when reviewing or recovering a Helmet task. It ships with the
skill and needs no external planning, review, or company skill pack. The issue,
repository rules, authority policy, and explicit operator decisions define the
accepted outcome. Additional reviewer preferences do not redefine it.

## Make the review converge

A blocking finding identifies a supported failure, its material consequence,
and the violated requirement or correctness/security boundary. Include the
smallest correction or observation that resolves it. Missing required runtime
acceptance can block readiness without being described as a code defect.

Consolidate related symptoms in the first pass. A repair review checks the new
head, prior findings, changed behavior, and affected boundaries. Reuse evidence
that still applies; broaden only for a new risk or missing required coverage.
Required checks must still pass for the current head. Explain any corrected
verdict on unchanged evidence.

Useful nits may be fixed cheaply or left nonblocking. Neither cosmetic changes
nor optional hardening need an owner waiver, a repair dispatch, or another
review round. Use the normal clean path when no material defect remains.
A false privacy claim or broken copyable command can block; harmless formatting
cannot. For human documentation, explain the subject in plain language, preserve
necessary context and uncertainty, and group comprehension problems instead of
serial copyediting. Use a design overview only where it helps explain a decision.

## Exercise the assumption

Before dependent implementation, name the risky assumption, cheapest real
probe, expected observation, and response if false. Exercise the actual worker
identity, repository access, API shape, or runtime command through authorized
interfaces. A thin command receipt comes before a reusable evidence framework.
Do not expose credentials or expand access to make a probe pass.

A validator must accept the contract's permitted alternatives as well as reject
bad cases. A clean PR need not have a repair. Several commits pushed together
are not automatically separately published heads. Code readiness, runtime
acceptance, historical workflow compliance, and publication authority are
separate claims. Record a late review honestly; do not fabricate a defect,
backdate approval, or demand a stronger demonstration than the accepted task.

## Replace a structurally failing approach

After two unsuccessful repairs of the same underlying assumption, diagnose the
approach before another repair request. A new unrelated defect or provider/CI
outage does not justify a rewrite. A parser collecting exceptions despite an
available supported parser is a reason to replace the mechanism.

A report-only reviewer provides a finding and successor outline. A Captain
with task-management authority performs the replacement:

1. Preserve the old PR, branch, commits, useful tests, and evidence. Prepare a
   distinct successor issue with the original outcome and the skeleton below.
2. Quiesce the old attempt through supported runtime/controller operations,
   including active execution and pending repair dispatch. Confirm that it
   cannot resume writing. Retire only that attempt's observer. Closing a PR or
   recording `blocked` alone does not cancel its worker.
3. Close the failed PR as superseded with the successor link. Keep the original
   objective visibly unfinished until its replacement delivers it. Do not
   delete checkpoint/ledger history, reset counters, or create a second root
   for the original issue.
4. Rewire affected epic membership/dependencies to the successor under existing
   authority, then explicitly accept the refreshed graph before new dispatch.
   A closed-as-incomplete predecessor is not a satisfied dependency. Revalidate
   merge authority; do not copy an old approval or widen a restrictive marker.
5. Dispatch the distinct successor through the normal issue/poller path on a
   fresh branch. The Captain supplies an interface/example in the issue; the
   worker writes the implementation in its own checkout. Reuse useful tests
   and interfaces, not the failed mechanism's accumulated patches.

Helmet's portable issue/transport API does not currently provide a cancellation
verb. Inspect the deployed controller's supported interface. If old execution
or future repair dispatch cannot be safely stopped, keep the prepared successor
undispatched and report that exact blocker. Do not invent a cancel command,
manipulate the database, or stop a service shared by unrelated tasks.

A successor skeleton fits in the issue body:

- Original observable outcome and failed assumption.
- Smallest replacement, starter interface, and representative fixture.
- First real probe and its expected result.
- Material regression plus a valid alternative that must remain supported.
- Useful work to preserve; optional work excluded.
- Required evidence, bounded budget, and failure response.
- Predecessor issue/PR and confirmation that the old attempt cannot write.

Restarting should reduce the problem. It must not reset the same speculative
plan under a new task name or treat PR closure as completion.
