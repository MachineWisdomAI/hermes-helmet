# Host continuation

Use this procedure whenever `helmet-issue` or `helmet-epic` reaches pending work,
including adoption, repair, and resumption after interruption. The existing
issue/epic checkpoint remains the delivery record. The native host owns waiting
and schedule persistence; this procedure adds no dispatcher or repair owner.

## Select and register

1. Inspect the host's exposed tools and existing background tasks/schedules.
   Use a native background task only when its completion automatically delivers
   a result to this First Officer and resumes the owning conversation. A session
   ID whose output must be checked with `write_stdin`, `TaskOutput`, repeated
   `wait` calls, or fresh model turns is a poll-only handle, not that capability.
2. With automatic delivery, adopt the existing bounded waiter for each active
   issue. Otherwise, adopt or create one native schedule for the owning issue or
   epic. On Codex with poll-only `exec_command`/`write_stdin`, use
   `automation_update` with `kind: heartbeat` and `destination: thread` in the
   current chat. Other hosts use their exposed scheduler, such as Claude Code's
   `CronCreate`/`CronList`/`CronDelete`. Use a native background-command facility
   on Claude Code only when it delivers completion to the First Officer.
3. Match a schedule by owning chat/session and exact issue or epic URL before
   creating it. An epic's schedule covers all active children; child invocations
   reuse it. Preserve the existing schedule ID and prompt scope on updates.
   Scheduled continuation replaces the process waiter. When switching, stop only
   this run's read-only wait handles; leave worker tasks running.
4. Read `details.first_officer_check_interval_minutes` from issue/epic status:
   its policy default is five minutes. An explicit per-run cadence wins and is
   stored in the native schedule. On resumption, retain that schedule's cadence;
   update the same schedule when the Captain changes it. This setting is separate
   from the worker poller's `schedule`. If the scheduler cannot represent the
   requested cadence, report that limitation rather than changing it silently.
5. Confirm the task or schedule registration succeeded before announcing its
   mechanism, handle/ID, and effective cadence. Use `--host-continuation session`
   for automatic completion delivery and `--host-continuation cron` for a native
   schedule. Announce once, then yield the turn. If neither mechanism is exposed
   or registration fails, use `--one-pass-only --host-continuation none`, report
   the limitation, and stop; do not promise background resumption.

## Automatic completion

Run one read-only `helmet wait ISSUE_URL --timeout-seconds 1800` per active child
through the native background facility. Keep its opaque cursor and host handle.
The host delivers completion; do useful independent work or yield in the
meantime. A user-requested status check may inspect once. Passive waiting does
not poll the handle, read worker logs, or emit unchanged progress messages.

Exit `0` means meaningful state changed; exit `2` is a clean bounded timeout.
Reconcile the owning issue or whole epic once, pass the returned cursor to the
next wait with `--cursor`, and rearm only still-pending children. Preserve other
active child handles until they finish; discard a completed handle's result if
it no longer belongs to the frontier. Exit `1` is a transport/contract failure:
report it and the concrete continuation limitation without a retry loop.

After interruption, inspect native task ownership and live delivery state once.
Adopt a surviving handle; if automatic delivery is unavailable, switch to the
single scheduled path. Never restart the worker merely to restore observation.

## Scheduled wake

Save a cohesive prompt naming the exact URL, `helmet-issue` or `helmet-epic`, the
accepted scope, existing authority, cadence override if any, and original budget
boundary. Each scheduled wake runs one reconciliation pass through that skill
with `--host-continuation cron`; it continues actionable authorized work, such as
reviewing a new head, then yields when work is pending. It does not arm a process
wait or another schedule inside that pass.

Keep the same schedule while waiting for worker output, repair, or required
checks. Remain silent when state is unchanged or non-actionable; notify only on
meaningful progress, completion, failure, or required human action. Scheduled
checks use a model pass at the configured interval; automatic completion avoids
those unchanged passes. Neither path uses model-managed sleeps or a shell
polling loop. Keep only secret-free handles, cursors, and correlation identifiers
in host metadata; task bodies, logs, credentials, and review prose stay out.

Remove this run's schedule/wait handles on completion, cancellation, original
budget expiry, or a blocker requiring human action with no independent work
left to continue. An open epic left for Captain closeout is not a reason to keep
checking. Report the final delivery state once. If cleanup fails, name the exact
remaining host handle or schedule; do not claim observation stopped.

On resumption, reconcile live tasks, checkpoints, and native schedules before
registering anything. Cadence changes and host restarts preserve original starts,
budget deadlines, merge choices, and task identity.

## Host references

- [Codex chat schedules](https://learn.chatgpt.com/docs/automations)
- [Claude Code background commands](https://code.claude.com/docs/en/tools-reference#background-commands)
- [Claude Code schedules](https://code.claude.com/docs/en/scheduled-tasks)
