"""Manage full native authorization tuples through the owning host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from nexus.cli.clients.rebac import RebacClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def rebac() -> None:
    """Manage native ReBAC tuples with an existing native admin key."""


def _tuple_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    for name in reversed(("subject_type", "subject_id", "relation", "object_type", "object_id")):
        fn = click.argument(name)(fn)
    fn = click.option("--subject-relation", default=None, help="Userset relation on the subject.")(
        fn
    )
    return click.option("--zone-id", required=True, envvar="NEXUS_ZONE_ID")(fn)


def _body(
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    zone_id: str,
    subject_relation: str | None,
) -> dict[str, str]:
    body = {
        "zone": zone_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "relation": relation,
        "object_type": object_type,
        "object_id": object_id,
    }
    if subject_relation is not None:
        body["subject_relation"] = subject_relation
    return body


@rebac.command("create")
@_tuple_options
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=RebacClient)
def create(client: RebacClient, **fields: Any) -> ServiceResult:
    """Grant the complete zone, object, relation and subject tuple."""
    return ServiceResult(data=client.grant(_body(**fields)), message="Relationship granted.")


@rebac.command("delete")
@_tuple_options
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=RebacClient)
def delete(client: RebacClient, **fields: Any) -> ServiceResult:
    """Revoke the complete tuple, including its optional subject relation."""
    return ServiceResult(data=client.revoke(_body(**fields)), human_formatter=console.print)


@rebac.command("list")
@click.option("--zone-id", required=True, envvar="NEXUS_ZONE_ID")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=RebacClient)
def list_tuples(client: RebacClient, zone_id: str) -> ServiceResult:
    """List the native tuples stored for the explicit zone."""
    return ServiceResult(data=client.list(zone_id), human_formatter=console.print)


def register_commands(cli: click.Group) -> None:
    """Register the native tuple command group."""
    cli.add_command(rebac)
