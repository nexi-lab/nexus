"""Filesystem Protocol (contracts tier).

Exports ``NexusFilesystem`` — the kernel syscall contract.
"""

from nexus.contracts.filesystem.filesystem_abc import NexusFilesystem

__all__ = ["NexusFilesystem"]
