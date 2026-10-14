# First Officer continuation behavior cases

Exercise these cases with the bundled issue/epic skills and shared host
continuation reference. Host registration and wake delivery are behavioral
observations; configuration and package tests alone do not prove them.

| Case | Initial state | Expected behavior |
| --- | --- | --- |
| Native completion | One pending issue; the background tool automatically delivers completion. | One bounded wait, no handle polls while pending, one reconciliation on delivery, cursor forwarded if rearmed. |
| Poll-only Codex | Pending issue; terminal returns a session ID and exposes `write_stdin`; same-chat heartbeat is available. | Register one five-minute heartbeat, announce successful registration, yield; no process waiter or inner polling loop. |
| Epic ownership | Two active children; an epic heartbeat already exists. | Adopt the epic's schedule; child calls create no schedules; each wake reconciles the entire frontier once. |
| Cadence change | Policy default is 12 minutes; current run explicitly chose seven; Captain changes it to ten. | Preserve seven on resume, then update the same schedule to ten; original start, authority, and worker schedule remain unchanged. |
| Interruption | A native waiter or schedule survives a host interruption. | Inspect once and adopt it; recover lost completion delivery through one schedule without redispatching the worker. |
| Unchanged wake | Scheduled pass sees the same pending worker state. | One pass, no unchanged user message, yield until the next native wake. |
| Terminal cleanup | Completed run, cancellation, original deadline expiry, or human blocker with no independent work. | Remove only this run's recurrence/wait handles and report once; leave worker tasks and other runs alone. |
| Unavailable host | Completion delivery and scheduler absent, or registration fails. | One truthful pass, `one_pass_only`, report the limitation; no promise of future monitoring. |

For the busy-loop regression, retain only redacted tool-call counts and native
handle IDs in the private run receipt. A native wait should generate zero
unchanged model polling calls; a scheduled fallback should generate at most one
reconciliation pass per configured interval.
