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
   To obtain a fresh full review (including at the same commit, for example
   after changing the model), remove and re-request the worker; each new
   request is a new review.

## Read the result

Use GitHub notifications and the existing `helmet issue` status/recovery
facilities; do not add a polling loop. Read the worker's formal review on the
PR: `APPROVE` or `REQUEST_CHANGES`, bound to the reviewed commit. The task
closeout holds the review URL, verdict, reviewed commit, and six-question
audit. A blocked task means missing evidence or access, never a verdict.

Before merging, compare the reviewed commit with the PR's current head. The
worker review does not create a repair task, authorize merge, or replace your
merge decision.
