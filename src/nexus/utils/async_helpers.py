"""Async utility helpers (Issue #2129).

Re-exports ``fire_and_forget`` from ``nexus.core.sync_bridge`` so that
bricks can use it without importing from ``nexus.core``.
"""

from nexus.core.sync_bridge import fire_and_forget as fire_and_forget
