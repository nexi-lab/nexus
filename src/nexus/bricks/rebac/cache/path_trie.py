"""PathTrie — O(depth) path ancestor grouping.

A trie specialized for filesystem paths, enabling:
- O(depth) ancestor lookup (vs linear dirname() loop)
- Efficient parent grouping for batch permission checks
- Prefix-based invalidation

Used by HierarchyPreFilterStrategy to group paths by parent
directory without repeated string operations.

Related: Issue #3192
"""

from typing import Any


class PathTrieNode:
    """A single node in the path trie."""

    __slots__ = ("children", "value", "is_terminal", "path")

    def __init__(self) -> None:
        self.children: dict[str, PathTrieNode] = {}
        self.value: Any = None
        self.is_terminal: bool = False
        self.path: str = ""


class PathTrie:
    """Trie specialized for filesystem paths.

    Segments paths by '/' separator for O(depth) operations.

    Example:
        >>> trie = PathTrie()
        >>> trie.insert("/workspace/project/src/main.py")
        >>> trie.insert("/workspace/project/src/utils.py")
        >>> trie.insert("/workspace/docs/readme.md")
        >>>
        >>> # Group by parent
        >>> groups = trie.group_by_parent()
        >>> # {"/workspace/project/src": ["main.py", "utils.py"],
        >>> #  "/workspace/docs": ["readme.md"]}
        >>>
        >>> # Get ancestors
        >>> ancestors = trie.get_ancestors("/workspace/project/src/main.py")
        >>> # ["/workspace/project/src", "/workspace/project", "/workspace", "/"]
    """

    def __init__(self) -> None:
        self._root = PathTrieNode()
        self._root.path = "/"
        self._size = 0

    def insert(self, path: str, value: Any = None) -> None:
        """Insert a path into the trie.

        Args:
            path: Filesystem path (e.g., "/workspace/project/file.py")
            value: Optional value to associate with the path
        """
        segments = self._split_path(path)
        node = self._root

        for segment in segments:
            if segment not in node.children:
                child = PathTrieNode()
                node.children[segment] = child
            node = node.children[segment]

        node.is_terminal = True
        node.value = value
        node.path = path
        self._size += 1

    def get_ancestors(self, path: str) -> list[str]:
        """Get all ancestor paths from immediate parent to root.

        O(depth) operation — single trie traversal.

        Args:
            path: Path to find ancestors for

        Returns:
            List of ancestor paths from immediate parent to root
        """
        segments = self._split_path(path)
        ancestors = ["/"]  # root is always an ancestor

        current_path = ""
        for i in range(len(segments) - 1):
            current_path = "/" + "/".join(segments[: i + 1])
            ancestors.append(current_path)

        ancestors.reverse()  # immediate parent first, root last

        return ancestors

    def group_by_parent(self) -> dict[str, list[str]]:
        """Group all terminal paths by their parent directory.

        O(n) traversal of all inserted paths.

        Returns:
            Dict mapping parent path -> list of child filenames
        """
        groups: dict[str, list[str]] = {}
        self._collect_groups(self._root, [], groups)
        return groups

    def find_nearest_ancestor(self, path: str, predicate: Any = None) -> str | None:
        """Find the nearest ancestor of path that exists in the trie.

        O(depth) operation.

        Args:
            path: Path to search from
            predicate: Optional function(node) -> bool to filter matches

        Returns:
            Nearest ancestor path, or None
        """
        segments = self._split_path(path)
        node = self._root
        nearest = None

        if self._root.is_terminal and (predicate is None or predicate(self._root)):
            nearest = "/"

        current_path_parts: list[str] = []
        for segment in segments:
            if segment not in node.children:
                break
            node = node.children[segment]
            current_path_parts.append(segment)
            if node.is_terminal:
                candidate = "/" + "/".join(current_path_parts)
                if predicate is None or predicate(node):
                    nearest = candidate

        return nearest

    def get_all_under(self, prefix: str) -> list[str]:
        """Get all paths under a prefix.

        Args:
            prefix: Path prefix

        Returns:
            List of all paths under the prefix
        """
        segments = self._split_path(prefix)
        node = self._root

        for segment in segments:
            if segment not in node.children:
                return []
            node = node.children[segment]

        results: list[str] = []
        self._collect_terminals(node, results)
        return results

    def clear(self) -> None:
        """Clear all entries."""
        self._root = PathTrieNode()
        self._root.path = "/"
        self._size = 0

    @property
    def size(self) -> int:
        """Number of terminal paths."""
        return self._size

    def _split_path(self, path: str) -> list[str]:
        """Split path into segments, filtering empty parts."""
        return [p for p in path.split("/") if p]

    def _collect_groups(
        self,
        node: PathTrieNode,
        prefix_parts: list[str],
        groups: dict[str, list[str]],
    ) -> None:
        """Recursively collect parent->children groups."""
        for name, child in node.children.items():
            child_parts = prefix_parts + [name]
            if child.is_terminal:
                parent = "/" + "/".join(prefix_parts) if prefix_parts else "/"
                groups.setdefault(parent, []).append(name)
            self._collect_groups(child, child_parts, groups)

    def _collect_terminals(self, node: PathTrieNode, results: list[str]) -> None:
        """Recursively collect all terminal paths under a node."""
        if node.is_terminal and node.path:
            results.append(node.path)
        for child in node.children.values():
            self._collect_terminals(child, results)
