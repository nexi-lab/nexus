"""AgentRuntimeRegistry — kernel-knows slot for in-process agent runtimes.

The sudo-code Rust crate is linked into nexusd and registers its
``AgentRuntime`` instance into this registry at module init. The
``SudoCodeRPCService.start_session`` handler consults the registry on
every spawn so the gRPC surface stays the same regardless of which
agent name the runtime handles.

Empty registry is OK — agents are still recorded in AgentRegistry on
spawn, the runtime task is just not driven. This lets the proto +
service contract land before the runtime crate is wired up.

Layering:

  ┌────────────────────────────────────────────────────────────────┐
  │  SudoCodeRPCService.sudo_code_start_session                    │
  │     → AgentRegistry.spawn(...)            (allocates pid)      │
  │     → AgentRuntimeRegistry.get(agent)     (kernel-knows slot)  │
  │     → runtime.spawn(pid, workspace, ...)  (in-process task)    │
  └────────────────────────────────────────────────────────────────┘

The trait surface is intentionally narrow: spawn / cancel only.
Prompt and event flow uses the chat-with-me VFS surface (the runtime
sys_watches /proc/{pid}/chat-with-me for incoming prompts and writes
responses to /agents/{user}/chat-with-me) — no SendPrompt /
SubscribeEvents methods on the runtime.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentRuntime(Protocol):
    """In-process runtime contract.

    Every implementation owns one tokio task per pid (or the equivalent
    in the host language). The runtime calls kernel syscalls directly
    — no IPC, no subprocess, no JSON-RPC — and mutates state through
    the same in-process channel every kernel observer uses.
    """

    def spawn(
        self,
        *,
        pid: str,
        workspace_path: str,
        repos: list[dict[str, Any]],
        model: str,
    ) -> None:
        """Start the runtime task for ``pid``.

        ``workspace_path`` is the resolved ``/proc/{pid}/workspace/`` root.
        ``repos`` is the list of ``{host_path, alias}`` dicts the
        provisioner has already materialised as OS-level symlinks.
        ``model`` is the optional model override (empty = use the
        agent's config.toml).

        Raises on failure — the caller will reap the AgentRegistry pid
        so the record does not drift.
        """

    def cancel(self, *, pid: str, mode: str) -> None:
        """Cancel an in-flight turn or terminate the runtime task.

        ``mode`` is one of ``"cancel_turn"`` (abort current generation,
        keep the task alive) or ``"cancel_session"`` (terminate the
        task — the caller will reap the AgentRegistry pid afterwards).
        """


class AgentRuntimeRegistry:
    """Name-keyed slot map of registered runtimes.

    The registry surface is small on purpose — it mirrors the
    NativeInterceptHook registration pattern already used elsewhere in
    nexusd. A runtime crate calls ``register(agent_name, instance)``
    once at module init; the gRPC handler calls ``get(agent_name)`` on
    every StartSession.
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}

    def register(self, agent_name: str, runtime: AgentRuntime) -> None:
        """Register ``runtime`` under ``agent_name``.

        Raises ``ValueError`` if a runtime is already registered for
        the name — re-registering would silently shadow a working
        runtime, which is harder to debug than a startup error.
        """
        if not agent_name:
            raise ValueError("agent_name is required")
        if agent_name in self._runtimes:
            raise ValueError(f"runtime already registered for agent {agent_name!r}")
        self._runtimes[agent_name] = runtime

    def unregister(self, agent_name: str) -> None:
        """Remove the runtime registered for ``agent_name``.

        No-op if no runtime is registered — callers (typically test
        teardown) should not have to track whether registration ever
        succeeded.
        """
        self._runtimes.pop(agent_name, None)

    def get(self, agent_name: str) -> AgentRuntime | None:
        """Return the registered runtime for ``agent_name`` or ``None``.

        ``SudoCodeRPCService`` treats ``None`` as "create the agent
        record but skip the runtime spawn" so the proto contract works
        before the runtime crate wires up.
        """
        return self._runtimes.get(agent_name)

    def list(self) -> list[str]:
        """Return the agent names currently bound to a runtime."""
        return list(self._runtimes)


__all__ = ["AgentRuntime", "AgentRuntimeRegistry"]
