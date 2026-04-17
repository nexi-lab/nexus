"""nexus chat — Agent REPL and one-shot mode (nexus-agent-plan §11.2).

Two interaction modes:
    nexus chat                  Interactive REPL (multi-turn)
    nexus chat -p "fix bug"    One-shot (single prompt, exit)

Two NexusFS modes:
    (default)                  Embedded in-process (CLUSTER profile, no nexusd)
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
    help="Deployment profile for embedded mode (default: cluster).",
)
@click.option(
    "--tools",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Mount external tool directories (repeatable).",
)
def chat(
    prompt: str | None,
    model: str | None,
    with_addr: str | None,
    continue_session: bool,
    resume: str | None,
    deployment_profile: str | None,
    tools: tuple[str, ...],
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
        )
    )


async def _run_chat(
    *,
    prompt: str | None,
    model: str | None,
    with_addr: str | None,
    continue_session: bool,
    resume: str | None,
    deployment_profile: str | None,
    tools: tuple[str, ...] = (),
) -> None:
    """Bootstrap NexusFS + ManagedAgentLoop, then run REPL or one-shot."""
    from pathlib import Path

    import nexus

    # ── Resolve model from env/config ──
    model = model or os.environ.get("NEXUS_LLM_MODEL", "gpt-4o")
    base_url = os.environ.get("NEXUS_LLM_BASE_URL")
    api_key = os.environ.get("NEXUS_LLM_API_KEY", "")

    if not base_url:
        click.echo(
            "Error: LLM backend URL required.\n"
            "Set NEXUS_LLM_BASE_URL environment variable or configure in ~/.nexus/config.yaml.",
            err=True,
        )
        sys.exit(1)

    # ── Bootstrap NexusFS ──
    if with_addr:
        # Remote mode: connect to existing nexusd
        nx = await nexus.connect(config={"profile": "remote", "url": f"http://{with_addr}"})
    else:
        # Embedded mode: in-process NexusFS (invocation-style, exclusive)
        profile = deployment_profile or os.environ.get("NEXUS_PROFILE", "cluster")
        state_dir = Path(getattr(nexus, "NEXUS_STATE_DIR", Path.home() / ".nexus"))
        data_dir = os.environ.get("NEXUS_DATA_DIR", str(state_dir / "data"))
        nx = await nexus.connect(config={"profile": profile, "data_dir": data_dir})

    try:
        # ── Mount LLM backend ──
        # Pure Rust — the openai_compatible mount is created directly by the
        # kernel. Rust-side OpenAIBackend owns HTTP, SSE decoding, CAS
        # persistence and DT_STREAM pump.
        from nexus.contracts.metadata import DT_MOUNT

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

        # ── Create agent loop ──
        from nexus.services.agent_runtime.compaction import DefaultCompactionStrategy
        from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop

        cwd = os.getcwd()
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
