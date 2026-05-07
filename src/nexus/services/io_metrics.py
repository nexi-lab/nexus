"""Compatibility re-export for the Nexus I/O metrics catalog.

The canonical module lives in :mod:`nexus.lib.io_metrics` so kernel-tier
modules can record I/O metrics without importing from the services tier.
"""

from nexus.lib.io_metrics import *  # noqa: F403
