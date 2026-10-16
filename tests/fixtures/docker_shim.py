#!/usr/bin/env python3
"""Test-only stand-in for ``docker compose -p P exec -T [-e K=V]... hermes CMD...``.

It records the argument vector, then runs ``CMD`` on this host with the ``-e``
variables applied, so the adapter chain can be exercised against a native Hermes
install without a Docker daemon. Real-Compose acceptance is the separate,
explicit integration test in ``tests/test_compose_runtime.py``.

Controls (environment of the shim itself, never of the container command):

- ``DOCKER_SHIM_LOG``   append one JSON line per call: argv, COMPOSE_* seen, stdin size
- ``DOCKER_SHIM_HOME``  ``HERMES_HOME`` for the command (an isolated Hermes home)
- ``DOCKER_SHIM_SRC``   host path substituted for ``/opt/hermes-helmet/src``
- ``DOCKER_SHIM_SLEEP`` seconds to sleep before running (timeout tests)
- ``DOCKER_SHIM_EXIT``  exit with this status without running anything
- ``DOCKER_SHIM_ECHO``  copy stdin to stdout byte for byte instead of running the command
"""

from __future__ import annotations

import json
import os
import sys
import time

CONTAINER_SRC = "/opt/hermes-helmet/src"


def main(argv: list[str]) -> int:
    log = os.environ.get("DOCKER_SHIM_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "argv": argv,
                        "compose_file": os.environ.get("COMPOSE_FILE"),
                        "compose_project": os.environ.get("COMPOSE_PROJECT_NAME"),
                    }
                )
                + "\n"
            )
    if os.environ.get("DOCKER_SHIM_EXIT"):
        return int(os.environ["DOCKER_SHIM_EXIT"])
    if os.environ.get("DOCKER_SHIM_SLEEP"):
        time.sleep(float(os.environ["DOCKER_SHIM_SLEEP"]))
    if os.environ.get("DOCKER_SHIM_ECHO"):
        sys.stdout.buffer.write(sys.stdin.buffer.read())
        sys.stdout.buffer.flush()
        return 0
    if argv[:1] != ["compose"] or argv[1:2] != ["-p"] or argv[3:5] != ["exec", "-T"]:
        sys.stderr.write("docker shim: unexpected invocation\n")
        return 64
    rest = argv[5:]
    # The container environment is what the image provides plus the -e values;
    # nothing leaks from the host profile variables.
    env = {"PATH": "/opt/hermes/.venv/bin:/usr/bin:/bin", "HOME": "/tmp"}
    if os.environ.get("DOCKER_SHIM_HOME"):
        env["HERMES_HOME"] = os.environ["DOCKER_SHIM_HOME"]
    index = 0
    while index < len(rest) and rest[index] == "-e":
        key, _, value = rest[index + 1].partition("=")
        if key == "PYTHONPATH":
            value = value.replace(CONTAINER_SRC, os.environ["DOCKER_SHIM_SRC"])
        env[key] = value
        index += 2
    service, command = rest[index], rest[index + 1 :]
    if service != "hermes":
        return 64
    os.execvpe(command[0], command, env)
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
