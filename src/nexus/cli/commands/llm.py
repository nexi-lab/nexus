"""LLM commands — start streaming LLM calls via kernel DT_STREAM.

Usage::

    nexus llm "What is 2+2?" --model gpt-4o
    nexus llm "Summarize this file" --model gpt-4o --stream-path /root/llm/.streams/my-session
"""

from __future__ import annotations

import asyncio
import json
import sys

import click

from nexus.cli.output import OutputOptions, add_output_options
from nexus.cli.utils import (
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register all LLM commands."""
    cli.add_command(llm)


@click.command()
@click.argument("prompt", type=str)
@click.option(
    "-m",
    "--model",
    type=str,
    default=None,
    help="Model name (e.g. gpt-4o, gpt-4o-mini). Uses backend default if not set.",
)
@click.option(
    "--stream-path",
    type=str,
    default=None,
    help="VFS path for the DT_STREAM. Auto-generated if not set.",
)
@click.option(
    "--no-stream",
    is_flag=True,
    help="Don't read streaming tokens; just print the stream path and exit.",
)
@add_output_options
@add_backend_options
def llm(
    prompt: str,
    model: str | None,
    stream_path: str | None,
    no_stream: bool,
    output_opts: "OutputOptions",
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Start a streaming LLM call.

    Sends a prompt to the mounted LLM backend and streams tokens back
    via a kernel DT_STREAM.

    \b
    Examples:
        nexus llm "What is 2+2?"
        nexus llm "Explain quantum computing" --model gpt-4o
        nexus llm "Hello" --no-stream
    """

    async def _impl() -> None:
        try:
            nx = await get_filesystem(remote_url, remote_api_key, allow_local_default=True)
        except Exception as e:
            handle_error(e)
            return

        import uuid

        # Build request
        messages = [{"role": "user", "content": prompt}]
        request: dict = {"messages": messages}
        if model:
            request["model"] = model

        # Generate stream path if not provided
        _stream_path = stream_path or f"/root/llm/.streams/{uuid.uuid4().hex[:12]}"

        try:
            # Rust-kernel-owned streaming. `nx.llm_start_streaming` blocks
            # the worker thread until the SSE completes, so we wrap it in
            # `asyncio.to_thread` to keep the event loop free for readers.
            request_bytes = json.dumps(request, separators=(",", ":")).encode("utf-8")
            llm_mount = "/llm" if _stream_path.startswith("/llm") else "/root/llm"
            _task = asyncio.create_task(
                asyncio.to_thread(
                    nx._kernel.llm_start_streaming,
                    llm_mount,
                    "root",
                    request_bytes,
                    _stream_path,
                )
            )
            # Small sleep to let the stream register before we tail-read.
            await asyncio.sleep(0)
            result = {"stream_path": _stream_path, "status": "streaming"}
        except Exception as e:
            handle_error(e)
            return

        if no_stream or getattr(output_opts, "json_output", False):
            # Just print the result and exit
            if getattr(output_opts, "json_output", False):
                click.echo(json.dumps(result, indent=2))
            else:
                console.print("[nexus.success]Stream started[/nexus.success]")
                console.print(f"  Stream path: {result.get('stream_path', _stream_path)}")
                console.print(f"  Status:      {result.get('status', 'unknown')}")
                console.print(
                    f"\n  Read tokens:  nexus cat {result.get('stream_path', _stream_path)}"
                )
            return

        # Stream tokens in real-time
        actual_path = result.get("stream_path", _stream_path)
        while True:
            try:
                data = nx.sys_read(actual_path, context=None)
                if not data:
                    break
                text = (
                    data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
                )

                # Check for control messages
                if text.startswith("{"):
                    try:
                        msg = json.loads(text)
                        if msg.get("type") == "done":
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            _model = msg.get("model", "unknown")
                            _latency = msg.get("latency_ms", 0)
                            console.print(f"\n[dim]model={_model} latency={_latency}ms[/dim]")
                            break
                        if msg.get("type") == "error":
                            console.print(
                                f"\n[nexus.error]Error:[/nexus.error] {msg.get('message')}"
                            )
                            break
                    except json.JSONDecodeError:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                else:
                    sys.stdout.write(text)
                    sys.stdout.flush()
            except Exception:
                # Stream closed or read error — done
                break

    asyncio.run(_impl())
