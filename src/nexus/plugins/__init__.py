"""Nexus Plugin System.

This module provides a plugin system for extending Nexus functionality
while maintaining vendor neutrality in the core.
"""

from nexus.plugins.base import NexusPlugin, PluginMetadata
from nexus.plugins.registry import PluginRegistry

__all__ = [
    "NexusPlugin",
    "PluginMetadata",
    "PluginRegistry",
]
