"""Nexus RLM — Recursive Language Model inference brick.

Enables agents to process near-infinite context (10M+ tokens) by
recursively decomposing and reasoning over data stored in Nexus VFS.
The model writes Python code in a REPL to programmatically inspect,
partition, and recursively reason over context slices.

Architecture:
    Agent → RLMInferenceService → SandboxManager (code execution)
                                → LiteLLMProvider (LLM calls)
                                → Nexus REST API (search/read via tools)

Key Design:
    - Context is NOT loaded into the model's window — the model uses
      nexus_read() and nexus_search() tools to lazily fetch what it needs
    - Each iteration: model generates code → sandbox executes → output
      shown to model → repeat until FINAL() or budget exceeded
    - Dedicated thread pool prevents RLM from starving other endpoints

Reference: arXiv:2512.24601 (Zhang, Kraska, Khattab — MIT OASYS Lab)
Related: Issue #1306, #1258 (MemGPT paging), #1271 (agent delegation)
"""

from nexus.rlm.manifest import RLMBrickManifest
from nexus.rlm.types import (
    REPLResult,
    RLMBudgetExceededError,
    RLMCodeError,
    RLMError,
    RLMInferenceRequest,
    RLMInferenceResult,
    RLMInfrastructureError,
    RLMIteration,
    RLMStatus,
    SSEEvent,
    SSEEventType,
)

__all__ = [
    # Types
    "RLMInferenceRequest",
    "RLMInferenceResult",
    "RLMIteration",
    "REPLResult",
    "RLMStatus",
    "SSEEvent",
    "SSEEventType",
    # Errors
    "RLMError",
    "RLMInfrastructureError",
    "RLMCodeError",
    "RLMBudgetExceededError",
    # Manifest
    "RLMBrickManifest",
]
