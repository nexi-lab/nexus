"""RLM CLI commands — recursive language model inference.

Maps to /api/v2/rlm/* endpoints via RLMClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.rlm import RLMClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


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
@service_command(client_class=RLMClient)
def rlm_infer(
    client: RLMClient,
    path: str,
    prompt: str,
    model: str | None,
    max_iterations: int | None,
) -> ServiceResult:
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
    data = client.infer(path, prompt=prompt, model=model, max_iterations=max_iterations)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        status = d.get("status", "unknown")
        answer = d.get("answer", d.get("result", ""))
        iterations = d.get("iterations", d.get("total_iterations", "N/A"))
        tokens = d.get("total_tokens", "N/A")

        console.print(f"[bold nexus.value]RLM Inference[/bold nexus.value] ({status})")
        console.print(f"  Iterations: {iterations}")
        console.print(f"  Tokens:     {tokens}")
        console.print()
        console.print(answer)

    return ServiceResult(data=data, human_formatter=_render)
