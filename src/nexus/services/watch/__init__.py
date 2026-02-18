"""Watch service — OS-native file change detection.

Provides cross-platform file watching using inotify (Linux) or
ReadDirectoryChangesW (Windows). macOS is not currently supported.

Moved from nexus.core.file_watcher (Issue #706).
"""

from nexus.services.watch.file_watcher import ChangeType, FileChange, FileWatcher

__all__ = ["ChangeType", "FileChange", "FileWatcher"]
