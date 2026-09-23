---
name: repo-bootstrap
description: "Example company-owned repository bootstrap skill for adopters (fixture only)."
version: 1.0.0
author: ExampleCo
---

# repo-bootstrap (ExampleCo fixture)

This is a **public fixture** showing the shape of an adopter-owned company skill.
It is not private Machine Wisdom content and must not embed credentials.

## Purpose

Help a Captain safely create or adopt a repository under the company GitHub
owner allowlist, establish branch and pull-request conventions, and optionally
install local git guards — without granting review or merge authority to the
executor.

## Boundaries

- Executor-only: never merge, never force-push, never impersonate the Captain.
- Do not copy private `mw-*` skills or internal runbooks into public packs.
- Do not store PATs, provider keys, or adopter identities in this file.

## Suggested steps (illustrative)

1. Confirm the target owner/repository is on the authority allowlist.
2. Create or clone the repository using the worker identity only.
3. Seed portable instruction files (`AGENTS.md`, contribution docs) from
   company-owned templates — not from private overlays.
4. Open a pull request for human or Captain review; do not self-merge.
