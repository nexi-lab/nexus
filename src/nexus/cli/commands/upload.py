"""Upload CLI commands — resumable upload management.

Maps to /api/v2/uploads/* (tus.io) endpoints via UploadClient.
Issue #2812. Note: `upload resume` is deferred to a future PR.
The tus protocol does not expose a list endpoint; use `upload status` per ID.
"""

from __future__ import annotations

import click

from nexus.cli.clients.upload import UploadClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


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
@service_command(client_class=UploadClient)
def upload_status(client: UploadClient, upload_id: str) -> ServiceResult:
    """Show upload progress.

    \b
    Examples:
        nexus upload status upl_123
        nexus upload status upl_123 --json
    """
    data = client.status(upload_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        offset = d.get("offset", 0)
        length = d.get("length", 0)
        pct = f"{offset / length * 100:.1f}%" if length > 0 else "N/A"

        console.print(f"[bold nexus.value]Upload: {upload_id}[/bold nexus.value]")
        console.print(f"  Progress: {pct} ({offset} / {length} bytes)")

    return ServiceResult(data=data, human_formatter=_render)


@upload.command("cancel")
@click.argument("upload_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=UploadClient)
def upload_cancel(client: UploadClient, upload_id: str) -> ServiceResult:
    """Cancel an in-progress upload.

    \b
    Examples:
        nexus upload cancel upl_123
    """
    data = client.cancel(upload_id)
    return ServiceResult(data=data, message=f"Upload {upload_id} cancelled")
