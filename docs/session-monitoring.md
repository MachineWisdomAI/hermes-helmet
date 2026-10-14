# Complete redacted session monitoring

`python -m hermes_helmet.session_snapshot` runs inside one Hermes profile. It
reads the native read-only SessionDB API and applies Hermes's force-mode session
redactor. It retains the complete native transfer projection, including inactive
sessions. It never copies or writes a session database.

Unchanged sessions reuse owner-only, redacted JSON cache entries under
`$HERMES_HOME/cache/helmet-session-snapshots`. The cache key hashes the complete
source record, redaction implementation, registered plugin patterns and scoped
vault redaction values. Source records and vault values are never persisted in
the cache. Changed messages, metadata or redaction rules invalidate the entry.
Deleted sessions are pruned after a complete successful pass. Unsafe cache
permissions or symlinks fail closed.

The default deadline is 20 seconds; `--timeout` overrides it. A cold cache may
need a longer first pass, for example `--timeout 180`, redirected to `/dev/null`
inside each profile during deployment acceptance. A failed pass retains only
completed redacted cache entries, allowing subsequent passes to continue warming.
Callers must check the exit status and discard all stdout on failure. A
multi-profile consumer should buffer and validate all results before publishing
one snapshot, with a single overall deadline. No history window or raw fallback
is used to meet the deadline.

Measured on the deployment's 535-session builder history, initial preparation
took 65 seconds and a subsequent full snapshot took 6.5 seconds. Export across
all five configured homes, including transferring the complete JSONL output,
took 16.9 seconds. These measurements are workload-specific, not a latency SLA.

Cron installation optionally accepts `delivery_target_env` in its reconciliation
and validation program builders. The generated program resolves the existing
target inside the worker and persists an explicit `platform:target` route on the
same job. A missing target aborts before mutation. It does not print the target
or embed it in source. This avoids relying on a gateway environment variable
remaining visible in a later profile-scoped cron execution.
