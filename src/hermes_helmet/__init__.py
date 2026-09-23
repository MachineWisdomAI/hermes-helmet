"""Hermes Helmet control-loop package."""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("Hermes Helmet requires Python 3.11 or later")

from hermes_helmet.authority import Policy, load_authority, load_policy, render_crew_contract

__version__ = "0.1.0rc1"

__all__ = [
    "Policy",
    "load_authority",
    "load_policy",
    "render_crew_contract",
    "__version__",
]
