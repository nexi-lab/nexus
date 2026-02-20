"""Watch service — OS-native file change detection.

Provides cross-platform file watching using watchfiles (Rust-backed).
Supports Linux, macOS, Windows.

Moved from nexus.core.file_watcher (Issue #706).
"""

from nexus.services.watch.file_watcher import ChangeType, FileChange, FileWatcher

__all__ = ["ChangeType", "FileChange", "FileWatcher"]
