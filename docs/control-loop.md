# Control loop

Hermes Helmet links a GitHub issue to a Hermes Kanban task, the resulting pull
request, and any repair requested during review. GitHub holds the development
record; Kanban holds worker assignments. The poller keeps those links current
so the worker can continue on the same change after feedback.

The runtime handles intake and repair in five parts:

1. **Issue and review-request intake** — poll allowlisted repositories for open
   issues carrying the configured dispatch label; create one Kanban root task
   per canonical issue URL. In the same pass, poll open pull requests in the
   allowlist for a pending request naming the configured worker among their
   reviewers, and create one ordinary Kanban task per request keyed by GitHub's
   `review_requested` event id. Review tasks carry the packaged review contract,
   omit the PR-publication completion contract, and never merge or repair. See
   [helmet-issue.md](helmet-issue.md#ask-hermes-to-review-a-pull-request).
2. **SQLite ledger** — durable `issue_tasks` and `pull_request_watches` tables
   under `/opt/data/github-issue-poller/ledger.sqlite3` (configurable).
3. **Pull-request discovery** — read current `published_pr` or legacy `pr_url`
   from completed Kanban run metadata, applying the same strict pull-request URL
   checks to both keys. Valid but different URLs fail closed rather than
   choosing one. A terminal root that first lands without either key keeps a
   null discovery sentinel and is re-read each poll so late canonical metadata
   can be adopted without ledger surgery; still-absent or closed late metadata
   stays quiet and does not replace the null sentinel.
4. **Trusted review filtering** — repository-role humans
   (`OWNER` / `MEMBER` / `COLLABORATOR` by default), plus bots listed in
   `trusted_review_bots`. Approvals and the configured worker automation
   identity are ignored.
5. **Same-PR repair** — dependent Kanban tasks reuse the root worktree and
   branch; the repair contract requires fetching current review comments,
   checks, and mergeability. No merge. No force-push. Poller root and repair
   tasks default to a GitHub PR completion contract (`OWNER/REPO`). Adopters on a
   private plan that cannot call the branch-rules API may set
   `worker_completion_contract: local-only`; workers still pass
   `metadata.published_pr`, and Captain merge gates stay independent of that
   hook.

Captain-side single-issue orchestration is layered on top without a second
watcher:

6. **helmet-issue** — portable skill + `helmet` CLI helpers that
   preflight Captain identity, adopt existing root tasks/PRs, wait for the
   worker PR, review each head, post formal GitHub findings, and apply the
   merge gate. Repair Kanban tasks remain owned exclusively by the issue poller.
   See [helmet-issue.md](helmet-issue.md).
7. **helmet-epic** — portable skill + `helmet epic` helpers that load a
   parent/child dependency graph (native sub-issues when available, else
   explicit `Parent` / `Blocked by` body links), compute the ready frontier,
   and invoke helmet-issue with bounded parallelism. The epic root is never
   dispatch-labeled. See [helmet-epic.md](helmet-epic.md).
8. **helmet-review** — portable skill that requests the configured worker as a
   GitHub reviewer on an existing pull request and reads its formal verdict
   from the review record and the task closeout, without a second watcher and
   without per-PR instructions. Issue implementation still uses the dispatch
   label; pull-request review uses the native review request.

Adopter configuration is one versioned authority policy (version 1 or
version 2). See `config/policy.example.json`, `docs/authority-schema.md`, and
`docs/private-overlay.md`. Machine Wisdom deployment values stay outside this
repository.
