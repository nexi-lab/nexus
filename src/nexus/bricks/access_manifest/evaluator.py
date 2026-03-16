"""Pure-function manifest evaluation (Issue #1754).

No I/O, no state — evaluates tool names against manifest entries.
First-match-wins semantics with case-insensitive fnmatch glob patterns.
Default: DENY (least privilege) when no entry matches.
"""

import fnmatch

from nexus.contracts.access_manifest_types import (
    EvaluationTrace,
    EvaluationTraceEntry,
    ManifestEntry,
    ToolPermission,
)


class ManifestEvaluator:
    """Evaluate tool access against manifest entries.

    All methods are pure functions (no side effects, no I/O).
    """

    @staticmethod
    def evaluate(entries: tuple[ManifestEntry, ...], tool_name: str) -> ToolPermission:
        """Evaluate a single tool against manifest entries.

        First-match-wins: iterates entries in order, returns the
        permission of the first matching entry. If no entry matches,
        returns DENY (least privilege).

        Args:
            entries: Ordered manifest entries.
            tool_name: Tool name to evaluate (case-insensitive).

        Returns:
            ToolPermission.ALLOW or ToolPermission.DENY.
        """
        normalized = tool_name.lower()
        for entry in entries:
            if fnmatch.fnmatch(normalized, entry.tool_pattern.lower()):
                return entry.permission
        return ToolPermission.DENY

    @staticmethod
    def evaluate_with_trace(entries: tuple[ManifestEntry, ...], tool_name: str) -> EvaluationTrace:
        """Evaluate a tool with a full decision trace (proof tree).

        Mirrors first-match-wins exactly: once a deciding entry is found,
        subsequent entries are recorded with matched=False (not evaluated,
        since evaluation stops at the first match).

        Args:
            entries: Ordered manifest entries.
            tool_name: Tool name to evaluate (case-insensitive).

        Returns:
            EvaluationTrace with the decision and per-entry trace.
        """
        normalized = tool_name.lower()
        trace_entries: list[EvaluationTraceEntry] = []
        matched_index = -1
        decision = ToolPermission.DENY

        for i, entry in enumerate(entries):
            # Once a match is found, stop evaluating — first-match-wins
            if matched_index == -1:
                matched = fnmatch.fnmatch(normalized, entry.tool_pattern.lower())
            else:
                matched = False

            trace_entries.append(
                EvaluationTraceEntry(
                    index=i,
                    tool_pattern=entry.tool_pattern,
                    permission=entry.permission,
                    matched=matched,
                    max_calls_per_minute=entry.max_calls_per_minute,
                )
            )
            if matched:
                matched_index = i
                decision = entry.permission

        return EvaluationTrace(
            tool_name=tool_name,
            decision=decision,
            matched_index=matched_index,
            entries=tuple(trace_entries),
            default_applied=matched_index == -1,
        )

    @staticmethod
    def filter_tools(
        entries: tuple[ManifestEntry, ...],
        tool_names: frozenset[str],
    ) -> frozenset[str]:
        """Filter a set of tool names, returning only allowed ones.

        Args:
            entries: Ordered manifest entries.
            tool_names: Set of tool names to filter.

        Returns:
            Frozenset of allowed tool names.
        """
        allowed: set[str] = set()
        for name in tool_names:
            normalized = name.lower()
            for entry in entries:
                if fnmatch.fnmatch(normalized, entry.tool_pattern.lower()):
                    if entry.permission == ToolPermission.ALLOW:
                        allowed.add(name)
                    break
            # No match → DENY (not added)
        return frozenset(allowed)
