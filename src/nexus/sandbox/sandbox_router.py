"""Smart sandbox routing: Monty (0.06ms) -> Docker (5s) -> E2B (20s) (Issue #1317).

Decides the cheapest execution path for agent code based on:
    1. STATIC ANALYSIS: AST-parse code for imports, I/O, system calls
    2. STICKY SESSION: Per-agent history overrides analysis
    3. FALLBACK ESCALATION: Catch EscalationNeeded, retry next tier

Design decisions:
    - #1: Separate module, injected via DI into SandboxManager
    - #2: Hybrid create-time default + run-time escalation
    - #3: AST-based via ast.parse()
    - #4: In-memory deque(maxlen=10) per agent
    - #7: EscalationNeeded exception for escalation signal
    - #15: LRU cache (maxsize=10,000) for agent history
"""

from __future__ import annotations

import ast
import logging
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from nexus.sandbox.sandbox_router_metrics import SandboxRouterMetrics

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# Tier priority: cheapest first
TIER_PRIORITY = ("monty", "docker", "e2b")

# Built-in functions safe for Monty (no I/O, no imports)
_MONTY_SAFE_BUILTINS = frozenset(
    {
        "print",
        "len",
        "range",
        "int",
        "str",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "type",
        "isinstance",
        "issubclass",
        "abs",
        "round",
        "min",
        "max",
        "sum",
        "sorted",
        "reversed",
        "enumerate",
        "zip",
        "map",
        "filter",
        "any",
        "all",
        "hasattr",
        "getattr",
        "setattr",
        "delattr",
        "id",
        "hash",
        "repr",
        "chr",
        "ord",
        "hex",
        "oct",
        "bin",
        "format",
        "iter",
        "next",
        "slice",
        "super",
        "property",
        "staticmethod",
        "classmethod",
        "object",
        "None",
        "True",
        "False",
        "NotImplemented",
        "Ellipsis",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "StopIteration",
        "KeyError",
        "IndexError",
        "AttributeError",
        "NameError",
        "ZeroDivisionError",
        "AssertionError",
        "Exception",
        "BaseException",
        "divmod",
        "pow",
        "complex",
        "bytes",
        "bytearray",
        "memoryview",
        "frozenset",
        "ascii",
        "breakpoint",
        "callable",
        "dir",
        "vars",
        "help",
        "input",
    }
)

# Functions that indicate non-trivial capabilities (need Docker/E2B)
_ESCALATION_FUNCTIONS = frozenset(
    {
        "open",
        "exec",
        "eval",
        "__import__",
        "compile",
        "globals",
        "locals",
    }
)

# History threshold: if >= this fraction of recent executions used a tier,
# sticky session overrides static analysis.
_HISTORY_THRESHOLD = 0.7


class SandboxRouter:
    """Smart routing: Monty (0.06ms) -> Docker (5s) -> E2B (20s).

    Three-level decision:
    1. STATIC ANALYSIS: AST-parse code for imports, I/O, system calls
    2. STICKY SESSION: Per-agent history overrides analysis
    3. FALLBACK ESCALATION: Catch EscalationNeeded, retry next tier
    """

    def __init__(
        self,
        available_providers: dict[str, SandboxProvider],
        history_maxlen: int = 10,
        agent_cache_maxsize: int = 10_000,
    ) -> None:
        if not available_providers:
            raise ValueError("No sandbox providers available. At least one is required.")

        self._providers = dict(available_providers)
        self._history_maxlen = history_maxlen
        self._agent_cache_maxsize = agent_cache_maxsize

        # Per-agent execution history: LRU-ordered dict of tier deques
        self._agent_history: OrderedDict[str, deque[str]] = OrderedDict()

        # Per-agent host function cache
        self._host_fn_cache: dict[str, dict[str, Callable[..., Any]]] = {}

        # Metrics
        self.metrics = SandboxRouterMetrics()

        logger.info(
            "SandboxRouter initialized (providers=%s, history_maxlen=%d)",
            list(self._providers.keys()),
            history_maxlen,
        )

    def analyze_code(self, code: str, language: str) -> str:
        """Static analysis -> tier name. Pure function, <1ms.

        Args:
            code: Source code to analyze.
            language: Programming language identifier.

        Returns:
            Tier name: "monty", "docker", or "e2b".
        """
        # Non-Python always needs Docker or E2B
        if language != "python":
            return self._first_available("docker", "e2b")

        # Empty/whitespace code is safe for Monty
        stripped = code.strip()
        if not stripped:
            return self._first_available("monty", "docker", "e2b")

        # Try AST parsing
        try:
            tree = ast.parse(stripped)
        except SyntaxError:
            # Let Monty report the syntax error if available
            return self._first_available("monty", "docker", "e2b")
        except Exception:
            # Any other parse failure (e.g., NUL bytes) — fall back
            return self._first_available("monty", "docker", "e2b")

        # Walk AST for escalation signals
        if self._needs_escalation(tree):
            return self._first_available("docker", "e2b")

        return self._first_available("monty", "docker", "e2b")

    def select_provider(self, code: str, language: str, agent_id: str | None) -> str:
        """Combined analysis + history -> provider name.

        Args:
            code: Source code to execute.
            language: Programming language.
            agent_id: Optional agent identifier for sticky sessions.

        Returns:
            Provider name to use.
        """
        # Static analysis
        analysis_tier = self.analyze_code(code, language)

        # Check agent history for sticky session
        if agent_id and agent_id in self._agent_history:
            history = self._agent_history[agent_id]
            if len(history) >= 3:  # Need minimum data points
                sticky_tier = self._get_sticky_tier(history)
                if sticky_tier and sticky_tier != analysis_tier and sticky_tier in self._providers:
                    logger.debug(
                        "Agent %s: sticky session -> %s (analysis suggested %s)",
                        agent_id,
                        sticky_tier,
                        analysis_tier,
                    )
                    return sticky_tier

        # Ensure the analysis tier is actually available
        if analysis_tier in self._providers:
            return analysis_tier

        # Fall through the priority chain
        return self._first_available(*TIER_PRIORITY)

    def record_execution(self, agent_id: str, tier: str, escalated: bool) -> None:
        """Record result in per-agent history.

        Args:
            agent_id: Agent identifier.
            tier: Tier that was used.
            escalated: Whether this was the result of an escalation.
        """
        if agent_id in self._agent_history:
            # Touch for LRU ordering
            self._agent_history.move_to_end(agent_id)
        else:
            # Enforce agent cache size limit (LRU eviction)
            if len(self._agent_history) >= self._agent_cache_maxsize:
                self._agent_history.popitem(last=False)
            self._agent_history[agent_id] = deque(maxlen=self._history_maxlen)

        self._agent_history[agent_id].append(tier)
        self.metrics.record_selection(tier)

        if escalated:
            logger.debug("Agent %s: recorded escalated execution on %s", agent_id, tier)

    def record_escalation(self, agent_id: str, from_tier: str, to_tier: str) -> None:
        """Record an escalation event.

        Args:
            agent_id: Agent identifier.
            from_tier: Tier that failed.
            to_tier: Tier escalated to.
        """
        self.metrics.record_escalation(from_tier, to_tier)
        # Record the target tier in history so future calls stick
        self.record_execution(agent_id, to_tier, escalated=True)
        logger.info(
            "Agent %s: escalation %s -> %s",
            agent_id,
            from_tier,
            to_tier,
        )

    def get_next_tier(self, current_tier: str) -> str | None:
        """Get next tier in escalation chain, or None.

        Only returns tiers that are actually available (registered).

        Args:
            current_tier: Current tier name.

        Returns:
            Next available tier name, or None if at the end.
        """
        try:
            idx = TIER_PRIORITY.index(current_tier)
        except ValueError:
            return None

        # Walk remaining tiers looking for one that's available
        for next_tier in TIER_PRIORITY[idx + 1 :]:
            if next_tier in self._providers:
                return next_tier
        return None

    def cache_host_functions(self, agent_id: str, host_fns: dict[str, Callable[..., Any]]) -> None:
        """Cache host functions for an agent (for re-wiring on escalation).

        Args:
            agent_id: Agent identifier.
            host_fns: Host function mapping.
        """
        self._host_fn_cache[agent_id] = dict(host_fns)

    def get_cached_host_functions(self, agent_id: str) -> dict[str, Callable[..., Any]] | None:
        """Get cached host functions for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            Host function mapping, or None if not cached.
        """
        return self._host_fn_cache.get(agent_id)

    # -- Internal helpers ---------------------------------------------------

    def _first_available(self, *tiers: str) -> str:
        """Return the first tier that has a registered provider.

        Args:
            *tiers: Tier names in preference order.

        Returns:
            First available tier name.

        Raises:
            ValueError: If none of the requested tiers are available.
        """
        for tier in tiers:
            if tier in self._providers:
                return tier
        # Should not happen if constructor validation passed
        return next(iter(self._providers))

    def _get_sticky_tier(self, history: deque[str]) -> str | None:
        """Check if history strongly favors a specific tier.

        Returns the tier if >= _HISTORY_THRESHOLD of recent executions
        used it, otherwise None.
        """
        if not history:
            return None

        total = len(history)
        counts: dict[str, int] = {}
        for tier in history:
            counts[tier] = counts.get(tier, 0) + 1

        for tier, count in counts.items():
            if count / total >= _HISTORY_THRESHOLD:
                return tier
        return None

    def _needs_escalation(self, tree: ast.Module) -> bool:
        """Walk AST looking for signals that need Docker/E2B.

        Checks for:
        - Import statements (import X, from X import Y)
        - Calls to open(), exec(), eval(), __import__(), compile()
        - Global/locals access
        """
        for node in ast.walk(tree):
            # Import statements
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return True

            # Function calls to escalation-triggering functions
            if isinstance(node, ast.Call):
                func_name = self._get_call_name(node)
                if func_name and func_name in _ESCALATION_FUNCTIONS:
                    return True

        return False

    @staticmethod
    def _get_call_name(node: ast.Call) -> str | None:
        """Extract the function name from an ast.Call node.

        Returns:
            Function name string, or None if it can't be determined.
        """
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None
