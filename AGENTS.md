# Hermes Helmet - AI Agent Instructions

## Project

Hermes Helmet is an open-source software factory for teams using coding agents.
It connects delegated work through implementation, review, repair, and acceptance.

Use **Hermes Helmet** as the full product name in prose and user-facing text.
The name evokes the mythical helmet of Hermes. Use `helmet` for the CLI;
preserve package, module, skill, path, and configuration identifiers.

## Shared Operating Rules

- Read the repo-local `AGENTS.md` first. Tool-specific files are additive and must not contradict it.
- Follow explicit operator instructions. Treat AI reviewer findings as evidence: verify and classify them before acting. Do not expand scope because a reviewer suggested it.
- Inspect the exact repo, branch, worktrees, and dirty state before editing. Preserve unrelated changes.
- Make tracked edits only on a dedicated worktree or feature branch. Keep the default checkout clean. Never commit or push directly to the default branch; land tracked changes through a pull request.
- Never force-push or amend a pushed commit. Stage explicit paths only; do not use `git add -A` or `git add .`.
- Resolve exact targets before destructive work. Do not discard changes, delete material data, merge, deploy, publish, post externally, or alter access without authority for that action.
- Never expose secrets. Keep provider credentials and user data within their authorized boundary.
- Treat `~/git/vendor/` checkouts as read-only. Develop in an owned repository or worktree.
- Shape ambiguous new builds before coding. Use an installed planning or discovery
  skill when one is available; otherwise record the problem, intended outcome,
  and boundaries in a repository-managed issue or document.
- Review non-trivial tracked changes before merge. Use an installed review skill
  when one is available and always run the repository's own validation. The
  self-contained delivery rules live in [Captain and Crew](docs/captain-and-crew.md#delivery-contract)
  and the bundled `helmet-issue` review/recovery reference. External skill packs
  are optional; material defects block, optional improvements do not.
- Keep durable non-trivial specs, decisions, status, and handoffs in
  repository-managed artifacts. Optional external skills and memory systems may
  augment that record but are never prerequisites for contributing.
- Do not build “shared-something” helper identities. Use shared-all or shared-none; give shared artifacts one owner and one documented repair path.

## Repo Notes

- Keep this file short and repo-specific.
- Put long runbooks, architecture, and operational detail in `docs/`.
