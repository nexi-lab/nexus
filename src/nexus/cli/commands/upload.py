"""Upload CLI commands — resumable upload management.

Uses inline httpx for tus.io protocol operations (HEAD/DELETE with
Tus-Resumable header). These do not map to standard RPC methods.
Issue #2812.
"""

from __future__ import annotations

import sys

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console

_TUS_HEADERS = {"Tus-Resumable": "1.0.0"}


def _get_base_url(remote_url: str | None) -> str:
    """Validate and return the base URL."""
    if not remote_url:
        console.print("[red]Error:[/red] Server URL required. Set NEXUS_URL or use --remote-url")
        sys.exit(1)
    return remote_url.rstrip("/")


@click.group()
def upload() -> None:
    """Resumable upload management.

    \b
    Inspect and cancel in-progress chunked uploads (tus.io protocol).

    \b
    Examples:
        nexus upload status <upload-id>
        nexus upload cancel <upload-id>
    """


@upload.command("status")
@click.argument("upload_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def upload_status(
    upload_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show upload progress.

    \b
    Examples:
        nexus upload status upl_123
        nexus upload status upl_123 --json
    """
    import httpx

    timing = CommandTiming()
    try:
        base_url = _get_base_url(remote_url)
        headers = dict(_TUS_HEADERS)
        if remote_api_key:
            headers["Authorization"] = f"Bearer {remote_api_key}"

        with timing.phase("server"):
            response = httpx.request(
                "HEAD",
                f"{base_url}/api/v2/uploads/{upload_id}",
                headers=headers,
                timeout=30.0,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Upload not found (HTTP {response.status_code})")
            data = {
                "upload_id": upload_id,
                "offset": int(response.headers.get("Upload-Offset", "0")),
                "length": int(response.headers.get("Upload-Length", "0")),
            }

        def _render(d: dict) -> None:
            offset = d.get("offset", 0)
            length = d.get("length", 0)
            pct = f"{offset / length * 100:.1f}%" if length > 0 else "N/A"

            console.print(f"[bold cyan]Upload: {upload_id}[/bold cyan]")
            console.print(f"  Progress: {pct} ({offset} / {length} bytes)")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@upload.command("cancel")
@click.argument("upload_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def upload_cancel(
    upload_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Cancel an in-progress upload.

    \b
    Examples:
        nexus upload cancel upl_123
    """
    import httpx

    timing = CommandTiming()
    try:
        base_url = _get_base_url(remote_url)
        headers = dict(_TUS_HEADERS)
        if remote_api_key:
            headers["Authorization"] = f"Bearer {remote_api_key}"

        with timing.phase("server"):
            response = httpx.request(
                "DELETE",
                f"{base_url}/api/v2/uploads/{upload_id}",
                headers=headers,
                timeout=30.0,
            )
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:
                    detail = response.text
                raise RuntimeError(f"Cancel failed (HTTP {response.status_code}): {detail}")
            data: dict = {}

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            message=f"Upload {upload_id} cancelled",
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
