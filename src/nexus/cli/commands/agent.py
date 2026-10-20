"""Manage transient runtime records in the native agent registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION
from nexus.contracts.process_types import AgentSignal, AgentState
from nexus.remote.agent_client import AgentClient


def _native_command(fn: Callable[..., ServiceResult]) -> Callable[..., None]:
    return add_output_options(
        REMOTE_API_KEY_OPTION(REMOTE_URL_OPTION(service_command(client_class=AgentClient)(fn)))
    )


def _labels(items: tuple[str, ...]) -> dict[str, str]:
    labels = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise click.BadParameter("Use KEY=VALUE with a non-empty key", param_hint="--label")
        labels[key] = value
    return labels


def _result(data: Any) -> ServiceResult:
    return ServiceResult(data=data, human_formatter=console.print)


@click.group()
def agent() -> None:
    """Manage native runtime registrations. Native credentials define authority."""


@agent.command("register")
@click.argument("name")
@click.option("--owner-id", help="Assert the authenticated owner; checked by the native host.")
@click.option("--zone-id", required=True, envvar="NEXUS_ZONE_ID")
@click.option("--connection-id", required=True)
@click.option("--host-pid", required=True, type=click.IntRange(min=1))
@click.option("--local-id", type=click.IntRange(min=0, max=2**32 - 1))
@click.option("--parent-pid")
@click.option("--label", "labels", multiple=True, help="Runtime label KEY=VALUE; repeatable.")
@_native_command
def register_cmd(client: AgentClient, labels: tuple[str, ...], **fields: Any) -> ServiceResult:
    """Register an external process; the daemon assigns its canonical PID.

    This creates a runtime record. Identity enrollment and native credentials
    belong to the runtime's existing enrollment flow.
    """
    return _result(client.register(**fields, labels=_labels(labels)))


@agent.command("list")
@click.option("--zone-id", envvar="NEXUS_ZONE_ID")
@click.option("--owner-id")
@click.option("--state", type=click.Choice([state.value for state in AgentState]))
@_native_command
def list_cmd(client: AgentClient, **filters: Any) -> ServiceResult:
    """List current native runtime records."""
    return _result(client.list(**filters))


@agent.command("info")
@click.argument("pid")
@_native_command
def info_cmd(client: AgentClient, pid: str) -> ServiceResult:
    """Read the current record for a canonical runtime PID."""
    return _result(client.get(pid))


@agent.command("delete")
@click.argument("pid")
@click.confirmation_option(prompt="Remove this external process from the runtime registry?")
@_native_command
def delete_cmd(client: AgentClient, pid: str) -> ServiceResult:
    """Unregister an external process. Credentials and files remain independent."""
    return _result({"unregistered": client.unregister(pid)})


@agent.command("transition")
@click.argument("pid")
@click.argument("state", type=click.Choice([state.value for state in AgentState]))
@_native_command
def transition_cmd(client: AgentClient, pid: str, state: str) -> ServiceResult:
    """Request a runtime state transition; the daemon validates its state machine."""
    return _result(client.update_state(pid, state))


@agent.command("heartbeat")
@click.argument("pid")
@_native_command
def heartbeat_cmd(client: AgentClient, pid: str) -> ServiceResult:
    """Refresh an external process heartbeat."""
    return _result(client.heartbeat(pid))


@agent.command("signal")
@click.argument("pid")
@click.argument("signal", type=click.Choice([signal.value for signal in AgentSignal]))
@click.option("--label", "labels", multiple=True, help="SIGUSR1 payload KEY=VALUE; repeatable.")
@_native_command
def signal_cmd(
    client: AgentClient, pid: str, signal: str, labels: tuple[str, ...]
) -> ServiceResult:
    """Send a native registry signal."""
    return _result(client.signal(pid, signal, _labels(labels)))
