"""nexus chat — Agent REPL, one-shot, and ACP mode.

Three interaction modes:
    nexus chat                  Interactive REPL (multi-turn)
    nexus chat -p "fix bug"    One-shot (single prompt, exit)
    nexus chat --acp            ACP mode: JSON-RPC over stdio (sudowork)

Two NexusFS modes:
    (default)                  Embedded in-process (slim profile, no nexusd)
    --with <addr>              Remote via REMOTE profile → existing nexusd

See: docs/architecture/nexus-agent-plan.md §11.2
     docs/architecture/cli-design.md
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import click


def _isolate_stdout_for_acp() -> Any:
    """Isolate stdout for ACP JSON-RPC: dup real stdout, redirect fd 1 -> stderr.

    After this call:
    - sys.stdout writes to stderr (catches Rust tracing, Python logging, print())
    - The returned stream writes to the original stdout fd (for ACP JSON-RPC only)
    - os.dup2 redirects fd 1 at the OS level so Rust tracing also goes to stderr
    """
    import io

    # Dup the real stdout fd before redirecting
    real_stdout_fd = os.dup(1)
    # Redirect fd 1 -> stderr at the OS level (catches Rust tracing)
    os.dup2(2, 1)
    # Redirect Python sys.stdout -> stderr (catches print/click.echo)
    sys.stdout = sys.stderr
    # Create a Python file object wrapping the real stdout fd
    return io.TextIOWrapper(
        io.FileIO(real_stdout_fd, mode="w", closefd=True),
        encoding="utf-8",
        line_buffering=True,
    )


@click.command("chat")
@click.option("-p", "--prompt", default=None, help="One-shot mode: run single prompt and exit.")
@click.option("--model", default=None, help="LLM model name.")
@click.option(
    "--with", "with_addr", default=None, help="Connect to existing nexusd (gRPC address)."
)
@click.option("--continue", "continue_session", is_flag=True, help="Resume most recent session.")
@click.option("--resume", default=None, help="Resume specific session by ID.")
@click.option(
    "--deployment-profile",
    type=click.Choice(["slim", "cluster", "embedded", "lite", "sandbox", "full", "cloud"]),
    default=None,
    help="Deployment profile for embedded mode (default: slim).",
)
@click.option(
    "--tools",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Mount external tool directories (repeatable).",
)
@click.option(
    "--acp",
    is_flag=True,
    default=False,
    help="ACP mode: JSON-RPC over stdio (for sudowork integration).",
)
def chat(
    prompt: str | None,
    model: str | None,
    with_addr: str | None,
    continue_session: bool,
    resume: str | None,
    deployment_profile: str | None,
    tools: tuple[str, ...],
    acp: bool,
) -> None:
    """Start an agent chat session.

    \b
    Interactive mode (default):
        nexus chat
        nexus chat --model gpt-4o
        nexus chat --with nexus-prod:2028

    \b
    One-shot mode:
        nexus chat -p "fix the login bug"
        nexus chat -p "add unit tests" --model claude-opus-4

    \b
    ACP mode (sudowork integration):
        nexus chat --acp
    """
    asyncio.run(
        _run_chat(
            prompt=prompt,
            model=model,
            with_addr=with_addr,
            continue_session=continue_session,
            resume=resume,
            deployment_profile=deployment_profile,
            tools=tools,
            acp=acp,
        )
    )


# ------------------------------------------------------------------
# Workspace mount + Tool registry wiring
# ------------------------------------------------------------------


async def _mount_workspace(nx: Any, cwd: str) -> None:
    """Mount host OS cwd at /workspace via LocalConnector."""
    from nexus.backends.storage.local_connector import LocalConnectorBackend
    from nexus.contracts.metadata import DT_MOUNT

    nx.sys_setattr("/workspace", entry_type=DT_MOUNT, backend=LocalConnectorBackend(cwd))


def _build_tool_registry(nx: Any, cwd: str) -> Any:
    """Build ToolRegistry with all 6 built-in tools (Tier A).

    All tools call async NexusFS syscalls.
    """
    from nexus.services.agent_runtime.tool_registry import ToolRegistry
    from nexus.services.agent_runtime.tools import (
        BashTool,
        EditFileTool,
        GlobTool,
        GrepTool,
        ReadFileTool,
        WriteFileTool,
    )

    async def _edit_fn(path: str, edit_pairs: list[tuple[str, str]]) -> dict:
        """Edit = read + patch + write (async)."""
        content = (await nx.sys_read(path)).decode("utf-8", errors="replace")
        for old, new in edit_pairs:
            if old not in content:
                return {"error": f"old_string not found in {path}"}
            content = content.replace(old, new, 1)
        await nx.write(path, content.encode("utf-8"))
        return {"status": "ok", "path": path}

    # SearchService for glob/grep
    from nexus.bricks.search.search_service import SearchService

    search = SearchService(metadata_store=nx.metadata)

    registry = ToolRegistry()
    registry.register(ReadFileTool(nx.sys_read))
    registry.register(WriteFileTool(nx.write))
    registry.register(EditFileTool(_edit_fn))
    registry.register(BashTool(cwd=cwd))
    registry.register(GlobTool(search))
    registry.register(GrepTool(search))
    return registry


# ------------------------------------------------------------------
# Main entrypoint
# ------------------------------------------------------------------


async def _run_chat(
    *,
    prompt: str | None,
    model: str | None,
    with_addr: str | None,
    continue_session: bool,
    resume: str | None,
    deployment_profile: str | None,
    tools: tuple[str, ...] = (),
    acp: bool = False,
) -> None:
    """Bootstrap NexusFS + ManagedAgentLoop, then run REPL or one-shot."""
    from pathlib import Path

    import nexus

    # ── ACP mode: isolate stdout for JSON-RPC ──
    acp_output = None
    if acp:
        acp_output = _isolate_stdout_for_acp()

    # ── Resolve LLM config from env ──
    # Priority: SUDOROUTER (Anthropic-native) > ANTHROPIC_API_KEY > NEXUS_LLM (OpenAI-compat)
    model = model or os.environ.get("NEXUS_LLM_MODEL")
    sr_base = os.environ.get("SUDOROUTER_BASE_URL")
    sr_key = os.environ.get("SUDOROUTER_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    oai_base = os.environ.get("NEXUS_LLM_BASE_URL")
    oai_key = os.environ.get("NEXUS_LLM_API_KEY", "")

    if not any([sr_base, anthropic_key, oai_base]):
        click.echo(
            "Error: LLM backend required.\n"
            "Set one of:\n"
            "  SUDOROUTER_BASE_URL + SUDOROUTER_API_KEY  (Anthropic via SudoRouter)\n"
            "  ANTHROPIC_API_KEY                          (Anthropic direct)\n"
            "  NEXUS_LLM_BASE_URL + NEXUS_LLM_API_KEY    (OpenAI-compatible)\n",
            err=True,
        )
        sys.exit(1)

    # ── Bootstrap NexusFS ──
    if with_addr:
        # Remote mode: connect to existing nexusd
        nx = await nexus.connect(config={"profile": "remote", "url": f"http://{with_addr}"})
    else:
        # Embedded mode: in-process NexusFS (invocation-style, exclusive)
        profile = deployment_profile or os.environ.get("NEXUS_PROFILE", "slim")
        state_dir = Path(getattr(nexus, "NEXUS_STATE_DIR", Path.home() / ".nexus"))
        data_dir = os.environ.get("NEXUS_DATA_DIR", str(state_dir / "data"))
        # ACP mode: each session gets its own data dir to avoid redb lock
        # conflicts with nexusd or other concurrent ACP sessions.
        if acp and "NEXUS_DATA_DIR" not in os.environ:
            import tempfile

            data_dir = tempfile.mkdtemp(prefix="nexus-acp-")
        nx = await nexus.connect(config={"profile": profile, "data_dir": data_dir})

    try:
        # ── Mount LLM backend (auto-detect driver from env) ──
        # Pure Rust — the kernel owns HTTP, SSE decoding, CAS persistence
        # and DT_STREAM pump. We just tell sys_setattr which backend to use.
        from nexus.contracts.metadata import DT_MOUNT

        base_url: str
        api_key: str
        if sr_base or (anthropic_key and not oai_base):
            # Anthropic-native driver (SudoRouter or direct Anthropic API)
            api_key = sr_key or anthropic_key or ""
            base_url = sr_base or "https://api.anthropic.com"
            model = model or "claude-sonnet-4-6"
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="anthropic",
                backend_name="anthropic_native",
                openai_base_url=base_url,
                openai_api_key=api_key,
                openai_model=model,
            )
        else:
            # OpenAI-compatible driver
            model = model or "gpt-4o"
            base_url = oai_base or ""
            api_key = oai_key
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="openai",
                backend_name="openai_compatible",
                openai_base_url=base_url,
                openai_api_key=api_key,
                openai_model=model,
            )

        # ── Mount external tool directories (Tier B, §1.5) ──
        if tools:
            from nexus.backends.storage.path_local import PathLocalBackend

            for tool_path in tools:
                tool_name = Path(tool_path).name
                mount_point = f"/root/tools/{tool_name}"
                tool_backend = PathLocalBackend(root_path=Path(tool_path))
                nx.sys_setattr(mount_point, entry_type=DT_MOUNT, backend=tool_backend)
                click.echo(f"  tools: {tool_path} → {mount_point}")

        # ── ACP mode: JSON-RPC over stdio for sudowork ──
        if acp:
            await _run_acp_mode(nx=nx, model=model, output=acp_output)
            return

        # ── Create agent loop (REPL / one-shot) ──
        from nexus.services.agent_runtime.compaction import DefaultCompactionStrategy
        from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop

        # Mount cwd via LocalConnector (REPL mode)
        cwd = os.getcwd()
        await _mount_workspace(nx, cwd)

        agent_path = "/root/agents/default"

        # Async wrappers for sync NexusFS syscalls — agent_runtime type
        # aliases require Awaitable return types for mypy strict mode.
        async def _async_sys_read(path: str) -> bytes:
            return nx.sys_read(path)

        async def _async_sys_write(path: str, buf: bytes) -> dict:
            return nx.sys_write(path, buf)

        # StreamManager stream_read for DT_STREAM token delivery
        _nx_stream_read = getattr(nx, "_stream_read", None)

        def _stream_read_adapter(path: str, offset: int) -> tuple[bytes, int]:
            if _nx_stream_read is None:
                raise NotImplementedError("Streaming not available in REMOTE mode")
            data = _nx_stream_read(path, offset=offset)
            return data, offset + len(data)

        # Bridge to Rust kernel: llm_start_streaming runs the full SSE →
        # DT_STREAM → CAS-persist pipeline in a worker thread so asyncio
        # can keep pumping the token reader without blocking.
        async def _llm_start_streaming(request_bytes: bytes, stream_path: str) -> None:
            await asyncio.to_thread(
                nx._kernel.llm_start_streaming, "/llm", "root", request_bytes, stream_path
            )

        loop = ManagedAgentLoop(
            sys_read=_async_sys_read,
            sys_write=_async_sys_write,
            stream_read=_stream_read_adapter,
            llm_start_streaming=_llm_start_streaming,
            agent_path=agent_path,
            llm_path="/llm",
            conv_path=f"{agent_path}/conversation",
            proc_path="/root/proc/chat-0",
            model=model,
            tool_registry=_build_tool_registry(nx, cwd),
            compactor=DefaultCompactionStrategy(
                sys_write=_async_sys_write,
                agent_path=agent_path,
            ),
            cwd=cwd,
        )

        await loop.initialize()

        # ── Session resume (--continue / --resume) ──
        _ = continue_session  # TODO: wire SessionManager.latest()
        _ = resume  # TODO: wire SessionManager.load(id)

        # ── One-shot or REPL ──
        if prompt:
            result = await loop.run(prompt)
            if result.text:
                click.echo(result.text)
        else:
            await _repl_loop(loop)

    finally:
        _close = getattr(nx, "close", None)
        if _close is not None:
            _close()


# ------------------------------------------------------------------
# ACP mode — JSON-RPC over stdio for sudowork integration
# ------------------------------------------------------------------


async def _run_acp_mode(
    *,
    nx: Any,
    model: str | None,
    output: Any | None = None,
) -> None:
    """Run in ACP mode — JSON-RPC over stdio for sudowork integration."""
    from nexus.services.agent_runtime.acp_handler import AcpProtocolHandler
    from nexus.services.agent_runtime.acp_transport import AcpTransport
    from nexus.services.agent_runtime.compaction import DefaultCompactionStrategy
    from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop
    from nexus.services.agent_runtime.observer import AgentObserver

    # Async wrappers for sync NexusFS syscalls (same as REPL mode)
    async def _async_sys_read(path: str) -> bytes:
        return nx.sys_read(path)

    async def _async_sys_write(path: str, buf: bytes) -> dict:
        return nx.sys_write(path, buf)

    # Stream read adapter
    _nx_stream_read = getattr(nx, "_stream_read", None)

    def _stream_read_adapter(path: str, offset: int) -> tuple[bytes, int]:
        if _nx_stream_read is None:
            raise NotImplementedError("Streaming not available")
        data = _nx_stream_read(path, offset=offset)
        return data, offset + len(data)

    # Bridge to Rust kernel LLM streaming
    async def _llm_start_streaming(request_bytes: bytes, stream_path: str) -> None:
        await asyncio.to_thread(
            nx._kernel.llm_start_streaming, "/llm", "root", request_bytes, stream_path
        )

    async def _loop_factory(session_id: str, cwd: str, observer: AgentObserver) -> ManagedAgentLoop:
        agent_path = "/root/agents/default"
        _cwd = cwd or os.getcwd()
        # Mount cwd via LocalConnector (ACP mode — cwd from session/new)
        await _mount_workspace(nx, _cwd)
        loop = ManagedAgentLoop(
            sys_read=_async_sys_read,
            sys_write=_async_sys_write,
            stream_read=_stream_read_adapter,
            llm_start_streaming=_llm_start_streaming,
            agent_path=agent_path,
            llm_path="/llm",
            conv_path=f"{agent_path}/conversation",
            proc_path=f"/root/proc/{session_id[:8]}",
            model=model,
            tool_registry=_build_tool_registry(nx, _cwd),
            compactor=DefaultCompactionStrategy(
                sys_write=_async_sys_write,
                agent_path=agent_path,
            ),
            cwd=_cwd,
        )
        loop._observer = observer
        await loop.initialize()
        return loop

    transport = AcpTransport(output=output)
    handler = AcpProtocolHandler(transport=transport, loop_factory=_loop_factory)
    await handler.run()


# ------------------------------------------------------------------
# Interactive REPL
# ------------------------------------------------------------------


async def _repl_loop(loop: Any) -> None:
    """Interactive REPL with slash commands."""
    click.echo("nexus agent (type /help for commands, /quit to exit)\n")

    while True:
        try:
            query = await asyncio.get_event_loop().run_in_executor(None, lambda: input("nexus > "))
        except (EOFError, KeyboardInterrupt):
            click.echo("\nBye.")
            break

        query = query.strip()
        if not query:
            continue

        # Slash commands
        if query.startswith("/"):
            cmd = query.split()[0].lower()
            if cmd in ("/quit", "/exit", "/q"):
                click.echo("Bye.")
                break
            if cmd == "/help":
                _print_help()
                continue
            if cmd == "/compact":
                from nexus.services.agent_runtime.compaction import estimate_tokens

                tokens = estimate_tokens(loop._messages)
                loop._messages = await loop._compactor.auto_compact(loop._messages)
                new_tokens = estimate_tokens(loop._messages)
                click.echo(f"Compacted: {tokens} → {new_tokens} tokens")
                continue
            if cmd == "/clear":
                loop._messages.clear()
                click.echo("Conversation cleared.")
                continue
            if cmd == "/cost":
                click.echo(f"Session: {loop.session_id}")
                click.echo(f"Messages: {len(loop._messages)}")
                from nexus.services.agent_runtime.compaction import estimate_tokens

                click.echo(f"Estimated tokens: {estimate_tokens(loop._messages)}")
                continue
            if cmd == "/status":
                click.echo(f"Session: {loop.session_id}")
                click.echo(f"Model: {loop._model}")
                click.echo(f"Messages: {len(loop._messages)}")
                continue
            click.echo(f"Unknown command: {cmd}. Type /help for available commands.")
            continue

        # Agent turn
        try:
            result = await loop.run(query)
            if result.text:
                click.echo(result.text)
            click.echo()
        except KeyboardInterrupt:
            click.echo("\n[interrupted]")
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)


def _print_help() -> None:
    click.echo(
        "Commands:\n"
        "  /help      Show this help\n"
        "  /compact   Compress conversation context\n"
        "  /clear     Clear conversation\n"
        "  /cost      Show token usage\n"
        "  /status    Show session status\n"
        "  /quit      Exit\n"
    )
