"""RLM CLI commands — recursive language model inference.

Maps to rlm_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group()
def rlm() -> None:
    """Recursive Language Model inference.

    \b
    Run RLM inference on files using iterative tool-augmented reasoning.

    \b
    Examples:
        nexus rlm infer /doc.pdf --prompt "Summarize this document"
    """


@rlm.command("infer")
@click.argument("path")
@click.option("--prompt", required=True, help="Inference prompt/query")
@click.option("--model", default=None, help="Model to use for inference")
@click.option("--max-iterations", type=int, default=None, help="Maximum reasoning iterations")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def rlm_infer(
    path: str,
    prompt: str,
    model: str | None,
    max_iterations: int | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Run RLM inference on a file.

    \b
    Note: This command currently waits for the full response. SSE streaming
    with progress display is planned for a future release.

    \b
    Examples:
        nexus rlm infer /doc.pdf --prompt "Summarize this document"
        nexus rlm infer /data/report.csv --prompt "What are the key trends?" --json
        nexus rlm infer /code.py --prompt "Find bugs" --max-iterations 5
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "rlm_infer",
                path=path,
                prompt=prompt,
                model=model,
                max_iterations=max_iterations,
            )

        def _render(d: dict) -> None:
            status = d.get("status", "unknown")
            answer = d.get("answer", d.get("result", ""))
            iterations = d.get("iterations", d.get("total_iterations", "N/A"))
            tokens = d.get("total_tokens", "N/A")

            console.print(f"[bold cyan]RLM Inference[/bold cyan] ({status})")
            console.print(f"  Iterations: {iterations}")
            console.print(f"  Tokens:     {tokens}")
            console.print()
            console.print(answer)

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
