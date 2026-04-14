"""LLM commands — start streaming LLM calls via generate_streaming().

Usage::

    nexus llm "What is 2+2?" --model gpt-4o
    nexus llm "Summarize this file" --model gpt-4o
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
    "--no-stream",
    is_flag=True,
    help="Collect full response before printing (no streaming output).",
)
@add_output_options
@add_backend_options
def llm(
    prompt: str,
    model: str | None,
    no_stream: bool,
    output_opts: "OutputOptions",
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Start a streaming LLM call.

    Sends a prompt to the mounted LLM backend and streams tokens back.

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

        # Build request
        messages = [{"role": "user", "content": prompt}]
        request: dict = {"messages": messages}
        if model:
            request["model"] = model

        try:
            # Find LLM backend via router
            _router = getattr(nx, "router", None)
            if _router is None:
                click.echo(
                    "Error: LLM streaming requires a local NexusFS (no router on remote client)",
                    err=True,
                )
                return
            route = _router.route("/root/llm")
            backend = route.backend

            if not hasattr(backend, "generate_streaming"):
                click.echo(
                    f"Error: Backend {type(backend).__name__} does not support generate_streaming",
                    err=True,
                )
                return

            # Iterate CC-format frames directly
            collected_text: list[str] = []
            result_usage: dict = {}

            for frame in backend.generate_streaming(request):
                ft = frame.get("type", "")
                if ft == "text":
                    text = frame["text"]
                    collected_text.append(text)
                    if not no_stream:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif ft == "usage":
                    result_usage = frame.get("usage", {})
                elif ft == "stop":
                    pass
                elif ft == "error":
                    console.print(f"\n[nexus.error]Error:[/nexus.error] {frame.get('message')}")
                    return

            if no_stream:
                sys.stdout.write("".join(collected_text))
                sys.stdout.flush()

            sys.stdout.write("\n")
            sys.stdout.flush()

            if getattr(output_opts, "json_output", False):
                click.echo(
                    json.dumps(
                        {
                            "text": "".join(collected_text),
                            "usage": result_usage,
                        },
                        indent=2,
                    )
                )
            else:
                _tokens = result_usage.get("total_tokens", 0)
                if _tokens:
                    console.print(f"\n[dim]tokens={_tokens}[/dim]")

        except Exception as e:
            handle_error(e)

    asyncio.run(_impl())
