---
name: helmet-review
description: "Request the configured Hermes worker to review a pull request through GitHub's native reviewer request, and read its formal verdict."
version: 1.0.0
author: Hermes Helmet
license: Apache-2.0
metadata:
  hermes:
    tags: [hermes-helmet, captain, review, github]
    related_skills: [helmet-issue]
---

# helmet-review

## Overview

`helmet-review` is the thin first-officer skill for asking Hermes to review an
existing pull request, for example one authored under the Captain's identity.
The worker-side poller turns GitHub's pending reviewer request into an ordinary
Kanban task carrying the packaged review contract. Hermes performs a full
review with its currently configured model and posts a formal GitHub review
under `worker_github_login`. You write no per-PR instructions.

## When to Use

- The Captain wants an independent worker review of a PR before merging.
- Do not use it to implement or repair (use `helmet-issue`). Issue
  implementation still uses the dispatch label; PR review uses the native
  review request only.

## Request a review

1. Read `worker_github_login` from the authority policy and confirm the PR's
   repository is in the policy repository allowlist. Any request GitHub
   permits (including fork PRs) is accepted; there is no requester filter.
2. Request the worker as a reviewer:

   ```sh
   gh pr edit PR_URL --add-reviewer WORKER_LOGIN
   ```

3. The next successful poll cycle creates one task. Repeated cycles reuse it.

## Repeat and recover

- **Push while pending.** Nothing to do. There is still one active review
  attempt per PR; the worker re-reads the live head before submitting and
  attaches the review to the commit it actually examined.
- **Push after a completed review.** Does not start another review.
- **Renewed review** (including at the same commit, or after changing the
  model): remove and re-request the worker. Each new request is a fresh full
  review that no earlier approval satisfies. If an attempt is still active, the
  renewed request is recorded at once as a task behind it (a Kanban child), so
  it is not lost when the earlier review is submitted.
- **Interrupted attempt.** Use the existing Kanban unblock/retry facilities on
  the task. If GitHub already accepted this request's review, the retried
  worker adopts the formal verdict carrying this request's
  `Helmet-Review-Request` line and records it without posting again. No manual
  instructions and no separate polling loop.

```sh
gh pr edit PR_URL --remove-reviewer WORKER_LOGIN
gh pr edit PR_URL --add-reviewer WORKER_LOGIN
```

## Read the result

Read the worker's formal review from GitHub itself:

```sh
gh api repos/OWNER/REPO/pulls/PR_NUMBER/reviews \
  --jq '.[] | {reviewer: .user.login, verdict: .state, commit: .commit_id, url: .html_url, submitted_at}'
```

`verdict` is `APPROVE` or `REQUEST_CHANGES`, bound to the reviewed `commit`.
Read the task's closeout through the existing Kanban task status and recovery
facilities, which hold the review URL, verdict, reviewed commit, and
six-question audit; a task in a blocked state means missing evidence or access,
never a verdict. Do not add a review/wait CLI verb, a watcher, or a polling
loop.

Before merging, compare the reviewed commit with the PR's current head. The
worker review does not create a repair task, authorize merge, or replace your
merge decision.
