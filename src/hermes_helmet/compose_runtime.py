"""``helmet-compose``: the public reference worker-runtime adapter for Docker Compose.

A first officer on the host reaches a native Hermes worker running in a
dedicated Docker Compose project through one executable. It serves two seams:

- ``HERMES_HELMET_HERMES=helmet-compose`` -- the ``hermes`` binary, so
  ``helmet-compose kanban ...`` runs the container's native ``hermes kanban``.
- ``HERMES_HELMET_WORKER_RUNTIME=helmet-compose`` -- the artifact command
  prefix, so ``artifact-upload|artifact-list|artifact-download BOARD TASK ...``
  reach the native attachment helper (:mod:`hermes_helmet.native_artifacts`).

Every call becomes ``docker compose -p PROJECT exec -T hermes ...`` and the
process is *replaced* by Docker with ``exec``: stdin/stdout stay the caller's own
file descriptors (binary-safe, nothing buffered or re-encoded here), the exit
status is Docker's (the container command's, so the documented 3/4/5 statuses
arrive unchanged), and the caller's timeout and process-group kill reach
Docker directly.

``COMPOSE_FILE`` and ``COMPOSE_PROJECT_NAME`` must both be set explicitly. They
are Docker-wide variables, so use a dedicated shell for the writing project and
never export them from a shell startup file. Nothing falls back to a default
project. The in-container actor is pinned to ``default`` regardless of any
``HERMES_PROFILE*`` variable on the host.

Only ``kanban`` and the three artifact verbs are supported. The coding workflow's
ledger, dispatch and wait verbs belong to a deployment's own adapter and are
refused here with exit status 2. A deployment adapter that keeps those verbs can
delegate the supported ones to :func:`build_command`.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

SERVICE = "hermes"
ACTOR = "default"
CONTAINER_PYTHON = "/opt/hermes/.venv/bin/python"
CONTAINER_HERMES = "/opt/hermes/.venv/bin/hermes"
CONTAINER_SOURCE = "/opt/hermes-helmet/src"
NATIVE_ARTIFACTS_MODULE = "hermes_helmet.native_artifacts"

KANBAN_VERB = "kanban"
ARTIFACT_VERBS = ("artifact-upload", "artifact-list", "artifact-download")
SUPPORTED_VERBS = (KANBAN_VERB, *ARTIFACT_VERBS)
# The coding workflow's verbs, named so the refusal is explicit rather than a
# generic "unknown verb". Any other verb is refused the same way.
COORDINATION_VERBS = (
    "ledger-root",
    "ledger-watch",
    "dispatch-root",
    "prepare-repo",
    "wait",
)

EXIT_USAGE = 2
EXIT_CONFIG = 2

# Variables that decide which Hermes identity the container command runs as.
_ACTOR_ENV = ("HERMES_PROFILE", "HERMES_PROFILE_NAME")


class ComposeRuntimeError(Exception):
    """A refusal with a fixed code and a fixed hint; never echoes arguments."""

    def __init__(self, code: str, hint: str, status: int = EXIT_USAGE) -> None:
        super().__init__(f"{code}: {hint}")
        self.code = code
        self.hint = hint
        self.status = status


def _require_project(environ: Mapping[str, str]) -> str:
    missing = [
        name
        for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME")
        if not (environ.get(name) or "").strip()
    ]
    if missing:
        raise ComposeRuntimeError(
            "compose_target_unset",
            "set COMPOSE_FILE and COMPOSE_PROJECT_NAME explicitly in a dedicated "
            "shell for the worker project; no default project is used",
            EXIT_CONFIG,
        )
    return environ["COMPOSE_PROJECT_NAME"].strip()


def _exec_prefix(project: str) -> list[str]:
    command = ["docker", "compose", "-p", project, "exec", "-T"]
    for name in _ACTOR_ENV:
        command += ["-e", f"{name}={ACTOR}"]
    return command


def build_command(
    argv: Sequence[str], environ: Mapping[str, str] | None = None
) -> list[str]:
    """Return the ``docker compose exec`` argument vector for one adapter call.

    Raises :class:`ComposeRuntimeError` for a missing Compose target or an
    unsupported verb. Arguments are passed as separate argv elements, never
    through a shell, and file content is never an argument (it travels on stdin).
    """

    env = os.environ if environ is None else environ
    args = list(argv)
    if not args:
        raise ComposeRuntimeError(
            "verb_required",
            "usage: helmet-compose kanban ... | artifact-upload|artifact-list|"
            "artifact-download BOARD TASK ...",
        )
    verb = args[0]
    if verb not in SUPPORTED_VERBS:
        raise ComposeRuntimeError(
            "unsupported_verb",
            "supported verbs are kanban, artifact-upload, artifact-list and "
            "artifact-download; ledger, dispatch and wait belong to a "
            "deployment adapter",
        )
    project = _require_project(env)
    prefix = _exec_prefix(project)
    if verb == KANBAN_VERB:
        return [*prefix, SERVICE, CONTAINER_HERMES, *args]
    return [
        *prefix,
        "-e",
        f"PYTHONPATH={CONTAINER_SOURCE}",
        SERVICE,
        CONTAINER_PYTHON,
        "-m",
        NATIVE_ARTIFACTS_MODULE,
        *args,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        command = build_command(args)
    except ComposeRuntimeError as exc:
        sys.stderr.write(f"helmet-compose: {exc.code}: {exc.hint}\n")
        return exc.status
    sys.stdout.flush()
    try:
        os.execvp(command[0], command)
    except OSError:
        # Docker is absent or not executable: report it the way a shell would,
        # which the artifact transport maps to "unavailable".
        sys.stderr.write("helmet-compose: docker_unavailable\n")
        return 127
    return 0  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    raise SystemExit(main())
