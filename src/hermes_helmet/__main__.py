"""python -m hermes_helmet entrypoint.

Default remains the H1 poller for backward compatibility with existing cron
installs. Captain-side commands live under ``python -m hermes_helmet.cli``.
"""

from __future__ import annotations

from hermes_helmet.github_issue_poller import main

raise SystemExit(main())
