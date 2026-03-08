"""Nexus CLI Federation Commands (Issue #2808).

High-level federation visibility and orchestration:
  federation status   - Show zone status (role, term, peers, cert expiry)
  federation list     - List all zones with topology overview
  federation discover - Probe a remote peer (health, version, TLS, latency)
  federation share    - Share a local subtree (pull model — local only)
  federation join     - Join a remote peer's shared subtree
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus.cli.utils import (
    ZoneConfig,
    add_zone_options,
    console,
    handle_error,
)

logger = logging.getLogger(__name__)

# Default gRPC address for client connections
DEFAULT_ADDR = "localhost:2126"
DEFAULT_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_channel(addr: str, zone_config: ZoneConfig | None = None) -> Any:
    """Create an async gRPC channel with optional mTLS."""
    import grpc
    from grpc import aio as grpc_aio

    channel_options = [
        ("grpc.keepalive_time_ms", 10000),
        ("grpc.keepalive_timeout_ms", 5000),
        ("grpc.keepalive_permit_without_calls", True),
        ("grpc.http2.max_pings_without_data", 0),
    ]

    tls_cfg = _resolve_tls_config(zone_config)
    if tls_cfg is not None:
        creds = grpc.ssl_channel_credentials(
            root_certificates=tls_cfg.ca_pem,
            private_key=tls_cfg.node_key_pem,
            certificate_chain=tls_cfg.node_cert_pem,
        )
        return grpc_aio.secure_channel(addr, creds, options=channel_options)
    return grpc_aio.insecure_channel(addr, options=channel_options)


def _resolve_tls_config(zone_config: ZoneConfig | None) -> Any:
    """Try to resolve TLS config from the zone data directory."""
    if zone_config is None:
        return None
    try:
        from nexus.security.tls.config import ZoneTlsConfig

        return ZoneTlsConfig.from_data_dir(zone_config.data_dir)
    except Exception:
        return None


async def _get_cluster_info(
    addr: str,
    zone_id: str,
    zone_config: ZoneConfig | None = None,
) -> dict[str, Any]:
    """Query cluster info for a zone via gRPC."""
    from nexus.raft import transport_pb2, transport_pb2_grpc

    channel = _build_channel(addr, zone_config)
    try:
        stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
        request = transport_pb2.GetClusterInfoRequest(zone_id=zone_id)
        response = await stub.GetClusterInfo(request, timeout=10.0)
        return {
            "node_id": response.node_id,
            "leader_id": response.leader_id,
            "term": response.term,
            "is_leader": response.is_leader,
            "leader_address": response.leader_address or None,
            "applied_index": response.applied_index,
        }
    finally:
        await channel.close()


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync Click context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Nested event loop — should not happen in CLI context
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _format_cert_status(zone_config: ZoneConfig | None) -> dict[str, Any] | None:
    """Check TLS cert expiry if TLS is configured."""
    tls_cfg = _resolve_tls_config(zone_config)
    if tls_cfg is None:
        return None
    try:
        from nexus.security.tls.certgen import check_cert_expiry

        status = check_cert_expiry(tls_cfg.node_cert_path)
        return {
            "days_remaining": status.days_remaining,
            "level": status.level,
        }
    except Exception:
        return None


def _discover_zones_from_disk(data_dir: str) -> list[str]:
    """Scan the data directory for zone subdirectories.

    Zone data is stored at ``{data_dir}/{zone_id}/``.  The Rust
    ``ZoneRaftRegistry`` starts with an empty in-memory map, so a
    freshly constructed ``ZoneManager`` would return no zones.
    Scanning the filesystem is the only way to discover existing
    zones from a CLI context without a running node.
    """
    base = Path(data_dir)
    if not base.is_dir():
        return []
    return sorted(
        entry.name for entry in base.iterdir() if entry.is_dir() and not entry.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# Command group
# ---------------------------------------------------------------------------


@click.group()
def federation() -> None:
    """Federation visibility and orchestration.

    Multi-node zone management: view topology, discover peers,
    share subtrees, and join remote zones.

    Status and list commands connect as a gRPC client to a running
    node (default: localhost:2126). Use --addr to specify a different
    target.
    """
    pass


# ---------------------------------------------------------------------------
# federation status <zone-id>
# ---------------------------------------------------------------------------


@federation.command(name="status")
@click.argument("zone_id", type=str)
@click.option("--addr", default=DEFAULT_ADDR, show_default=True, help="gRPC address of target node")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Timeout in seconds",
)
@add_zone_options
def status_cmd(
    zone_id: str,
    addr: str,
    json_output: bool,
    timeout: int,
    zone_config: ZoneConfig,
) -> None:
    """Show detailed status for a zone.

    Connects to a running node via gRPC to query cluster info.

    Examples:
        nexus federation status zone-a

        nexus federation status zone-a --addr peer1:2126 --json
    """
    try:

        async def _get_status() -> dict[str, Any]:
            return await asyncio.wait_for(
                _get_cluster_info(addr, zone_id, zone_config),
                timeout=timeout,
            )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Querying zone status...", total=None)
            info = _run_async(_get_status())

        # Enrich with cert status
        cert_info = _format_cert_status(zone_config)

        result = {
            "zone_id": zone_id,
            "node_id": info.get("node_id"),
            "leader_id": info.get("leader_id"),
            "term": info.get("term"),
            "is_leader": info.get("is_leader"),
            "leader_address": info.get("leader_address"),
            "applied_index": info.get("applied_index", 0),
            "role": "Leader" if info.get("is_leader") else "Follower",
            "tls": cert_info,
        }

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print()
            console.print(f"[bold]Zone:[/bold] {zone_id}")
            console.print(f"  Role:       {result['role']}")
            console.print(f"  Term:       {result['term']}")
            console.print(f"  Node ID:    {result['node_id']}")
            console.print(f"  Leader ID:  {result['leader_id']}")
            console.print(f"  Applied:    {result['applied_index']}")
            if result["leader_address"]:
                console.print(f"  Leader:     {result['leader_address']}")

            if cert_info:
                level = cert_info["level"]
                days = cert_info["days_remaining"]
                color = {"OK": "green", "WARN": "yellow", "CRITICAL": "red", "EXPIRED": "red"}[
                    level
                ]
                console.print(f"  TLS:        mTLS (cert expires in [{color}]{days}d[/{color}])")
            else:
                console.print("  TLS:        [dim]not configured[/dim]")

    except Exception as e:
        handle_error(e)


# ---------------------------------------------------------------------------
# federation list
# ---------------------------------------------------------------------------


@federation.command(name="list")
@click.option("--addr", default=DEFAULT_ADDR, show_default=True, help="gRPC address of target node")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Timeout in seconds",
)
@add_zone_options
def list_cmd(
    addr: str,
    json_output: bool,
    timeout: int,
    zone_config: ZoneConfig,
) -> None:
    """List all local zones with topology overview.

    Discovers zones from the local data directory, then queries
    the running node for live cluster info (parallel queries).

    Examples:
        nexus federation list

        nexus federation list --addr peer1:2126 --json
    """
    try:
        zones = _discover_zones_from_disk(zone_config.data_dir)

        if not zones:
            if json_output:
                click.echo(json.dumps([], indent=2))
            else:
                console.print("[dim]No zones found[/dim]")
            return

        async def _get_all_info() -> list[dict[str, Any]]:
            """Query cluster info for all zones in parallel."""
            from nexus.raft import transport_pb2, transport_pb2_grpc

            channel = _build_channel(addr, zone_config)
            try:
                stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
                tasks = [
                    asyncio.wait_for(
                        stub.GetClusterInfo(
                            transport_pb2.GetClusterInfoRequest(zone_id=z),
                            timeout=10.0,
                        ),
                        timeout=timeout,
                    )
                    for z in zones
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await channel.close()

            items = []
            for zone_id, result in zip(zones, results, strict=True):
                if isinstance(result, BaseException):
                    items.append(
                        {
                            "zone_id": zone_id,
                            "status": "ERROR",
                            "error": str(result),
                        }
                    )
                else:
                    items.append(
                        {
                            "zone_id": zone_id,
                            "role": "Leader" if result.is_leader else "Follower",
                            "term": result.term,
                            "node_id": result.node_id,
                            "leader_id": result.leader_id,
                            "is_leader": result.is_leader,
                            "leader_address": result.leader_address or "",
                            "status": "OK",
                        }
                    )
            return items

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(f"Querying {len(zones)} zone(s)...", total=None)
            items = _run_async(_get_all_info())

        # Enrich with cert status
        cert_info = _format_cert_status(zone_config)
        for item in items:
            item["tls"] = cert_info

        if json_output:
            click.echo(json.dumps(items, indent=2))
        else:
            console.print()
            table = Table(title="Federation Zones")
            table.add_column("Zone ID", style="cyan")
            table.add_column("Role")
            table.add_column("Term", justify="right")
            table.add_column("Status")
            if cert_info:
                table.add_column("TLS")

            for item in sorted(items, key=lambda x: x["zone_id"]):
                status = item["status"]
                status_style = "green" if status == "OK" else "red"
                role = item.get("role", "N/A")
                term = str(item.get("term", "N/A"))

                row = [item["zone_id"], role, term, f"[{status_style}]{status}[/{status_style}]"]

                if cert_info:
                    level = cert_info["level"]
                    days = cert_info["days_remaining"]
                    color = {"OK": "green", "WARN": "yellow", "CRITICAL": "red", "EXPIRED": "red"}[
                        level
                    ]
                    row.append(f"[{color}]{days}d ({level})[/{color}]")

                table.add_row(*row)

            console.print(table)

    except Exception as e:
        handle_error(e)


# ---------------------------------------------------------------------------
# federation discover <peer>
# ---------------------------------------------------------------------------


@federation.command(name="discover")
@click.argument("peer", type=str)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Timeout in seconds",
)
@add_zone_options
def discover_cmd(
    peer: str,
    json_output: bool,
    timeout: int,
    zone_config: ZoneConfig,
) -> None:
    """Probe a remote peer for health, version, zones, TLS, and latency.

    Runs a series of diagnostic checks against a remote node.

    Examples:
        nexus federation discover peer1:2126

        nexus federation discover peer1:2126 --json
    """
    try:
        results: dict[str, Any] = {"peer": peer, "checks": {}}

        async def _probe() -> None:
            import grpc

            from nexus.raft import transport_pb2, transport_pb2_grpc

            channel = _build_channel(peer, zone_config)

            # Probe 1: TCP-level connectivity via channel_ready()
            try:
                await asyncio.wait_for(channel.channel_ready(), timeout=10.0)
                results["checks"]["connection"] = {"status": "OK"}
            except Exception as exc:
                results["checks"]["connection"] = {
                    "status": "FAIL",
                    "error": str(exc),
                }
                await channel.close()
                return

            stub = transport_pb2_grpc.ZoneApiServiceStub(channel)

            # Probe 2: gRPC service reachability — call GetClusterInfo
            # with empty zone_id.  The server will return NOT_FOUND
            # (no zone named ""), but any application-level gRPC error
            # still proves the service is responding.  Only transport-
            # level failures (UNAVAILABLE, DEADLINE_EXCEEDED) count as
            # a real failure.
            try:
                try:
                    resp = await stub.GetClusterInfo(
                        transport_pb2.GetClusterInfoRequest(zone_id=""),
                        timeout=10.0,
                    )
                    results["checks"]["cluster_info"] = {
                        "status": "OK",
                        "node_id": resp.node_id,
                        "is_leader": resp.is_leader,
                    }
                except grpc.aio.AioRpcError as rpc_err:
                    code = rpc_err.code()
                    if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                        results["checks"]["cluster_info"] = {
                            "status": "FAIL",
                            "error": str(rpc_err),
                        }
                    else:
                        # NOT_FOUND, INVALID_ARGUMENT, etc. — service is alive
                        results["checks"]["cluster_info"] = {
                            "status": "OK",
                            "note": f"service responded with {code.name}",
                        }
            except Exception as exc:
                results["checks"]["cluster_info"] = {
                    "status": "FAIL",
                    "error": str(exc),
                }

            # Probe 3: gRPC RTT (3 round-trips via GetClusterInfo)
            # Any gRPC response — including NOT_FOUND — is a valid
            # round-trip measurement.
            rtts: list[float] = []
            for _ in range(3):
                t0 = time.monotonic()
                try:
                    await stub.GetClusterInfo(
                        transport_pb2.GetClusterInfoRequest(zone_id=""),
                        timeout=10.0,
                    )
                except grpc.aio.AioRpcError as rpc_err:
                    code = rpc_err.code()
                    if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                        break
                    # Application-level error is still a valid RTT sample
                except Exception:
                    break
                rtts.append((time.monotonic() - t0) * 1000)  # ms

            if rtts:
                results["checks"]["grpc_rtt_ms"] = {
                    "status": "OK",
                    "min": round(min(rtts), 2),
                    "avg": round(sum(rtts) / len(rtts), 2),
                    "max": round(max(rtts), 2),
                    "samples": len(rtts),
                }
            else:
                results["checks"]["grpc_rtt_ms"] = {
                    "status": "FAIL",
                    "error": "no successful pings",
                }

            # Probe 3: TLS certificate details
            tls_cfg = _resolve_tls_config(zone_config)
            if tls_cfg is not None:
                try:
                    from nexus.security.tls.certgen import check_cert_expiry

                    status = check_cert_expiry(tls_cfg.node_cert_path)
                    results["checks"]["tls"] = {
                        "status": "OK",
                        "mode": "mTLS",
                        "cert_days_remaining": status.days_remaining,
                        "cert_level": status.level,
                    }
                except Exception as exc:
                    results["checks"]["tls"] = {"status": "FAIL", "error": str(exc)}
            else:
                results["checks"]["tls"] = {"status": "N/A", "mode": "insecure"}

            await channel.close()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(f"Probing {peer}...", total=None)
            _run_async(asyncio.wait_for(_probe(), timeout=timeout))

        if json_output:
            click.echo(json.dumps(results, indent=2))
        else:
            console.print()
            console.print(f"[bold]Peer:[/bold] {peer}")
            console.print()

            for check_name, check_data in results["checks"].items():
                status = check_data.get("status", "UNKNOWN")
                color = {"OK": "green", "FAIL": "red", "N/A": "dim"}.get(status, "yellow")
                console.print(f"  [{color}]{status:4s}[/{color}]  {check_name}")

                for k, v in check_data.items():
                    if k == "status":
                        continue
                    console.print(f"         {k}: {v}")

    except Exception as e:
        handle_error(e)


# ---------------------------------------------------------------------------
# federation share (pull model — local only)
# ---------------------------------------------------------------------------


@federation.command(name="share")
@click.argument("local_path", type=str)
@click.option("--zone-id", type=str, default=None, help="Explicit zone ID (auto UUID if omitted)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Timeout in seconds",
)
@add_zone_options
def share_cmd(
    local_path: str,
    zone_id: str | None,
    json_output: bool,
    timeout: int,
    zone_config: ZoneConfig,
) -> None:
    """Share a local subtree by creating a new zone (pull model).

    Purely local operation. The remote peer joins later via
    ``nexus federation join``.

    Examples:
        nexus federation share /my/projects

        nexus federation share /data/models --zone-id ml-models --json
    """
    try:

        async def _share() -> str:
            from nexus.raft.federation import NexusFederation
            from nexus.raft.zone_manager import ZoneManager

            mgr = ZoneManager(
                node_id=zone_config.node_id,
                base_path=zone_config.data_dir,
                bind_addr=zone_config.bind_addr,
            )
            fed = NexusFederation(zone_manager=mgr)
            try:
                return await asyncio.wait_for(
                    fed.share(local_path, zone_id=zone_id),
                    timeout=timeout,
                )
            finally:
                mgr.shutdown()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(f"Sharing {local_path}...", total=None)
            new_zone_id = _run_async(_share())

        result = {
            "zone_id": new_zone_id,
            "local_path": local_path,
        }

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print(f"[green]Shared {local_path}[/green]")
            console.print(f"  Zone: {new_zone_id}")
            console.print(
                "  Peers can join with: nexus federation join "
                f"<this-node>:2126:{local_path} /local/mount"
            )

    except TimeoutError:
        console.print(f"[red]Timeout:[/red] Operation exceeded {timeout}s")
        sys.exit(1)
    except Exception as e:
        handle_error(e)


# ---------------------------------------------------------------------------
# federation join
# ---------------------------------------------------------------------------


@federation.command(name="join")
@click.argument("peer_spec", type=str)
@click.argument("local_path", type=str)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Timeout in seconds",
)
@add_zone_options
def join_cmd(
    peer_spec: str,
    local_path: str,
    json_output: bool,
    timeout: int,
    zone_config: ZoneConfig,
) -> None:
    """Join a remote peer's shared subtree.

    PEER_SPEC format: host:port:/remote/path

    Discovers the zone at the remote path, joins the Raft group,
    and creates a local mount.

    Examples:
        nexus federation join peer1:2126:/shared-projects /local/mount

        nexus federation join peer2:2126:/ml/models /data/shared --json
    """
    try:
        peer_addr, remote_path = _parse_peer_spec(peer_spec)

        async def _join() -> str:
            from nexus.raft.federation import NexusFederation
            from nexus.raft.zone_manager import ZoneManager

            mgr = ZoneManager(
                node_id=zone_config.node_id,
                base_path=zone_config.data_dir,
                bind_addr=zone_config.bind_addr,
            )
            fed = NexusFederation(zone_manager=mgr)
            try:
                return await asyncio.wait_for(
                    fed.join(peer_addr, remote_path, local_path),
                    timeout=timeout,
                )
            finally:
                mgr.shutdown()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(f"Joining {peer_addr}:{remote_path}...", total=None)
            zone_id = _run_async(_join())

        result = {
            "zone_id": zone_id,
            "peer": peer_addr,
            "remote_path": remote_path,
            "local_path": local_path,
        }

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print(f"[green]Joined zone {zone_id} from {peer_addr}[/green]")
            console.print(f"  Local mount: {local_path}")
            console.print(f"  Remote path: {remote_path}")

    except TimeoutError:
        console.print(f"[red]Timeout:[/red] Operation exceeded {timeout}s")
        sys.exit(1)
    except Exception as e:
        handle_error(e)


# ---------------------------------------------------------------------------
# Peer spec parser
# ---------------------------------------------------------------------------


def _parse_peer_spec(spec: str) -> tuple[str, str]:
    """Parse 'host:port:/remote/path' into (host:port, /remote/path).

    The first ':/' sequence separates the address from the path.
    This allows IPv4 host:port addresses while disambiguating the path.

    Raises:
        click.BadParameter: If the spec cannot be parsed.
    """
    # Find ':/' which marks the start of the path
    idx = spec.find(":/")
    if idx == -1:
        raise click.BadParameter(
            f"Invalid peer spec: '{spec}'. Expected format: host:port:/remote/path",
            param_hint="PEER_SPEC",
        )

    addr = spec[:idx]
    path = spec[idx + 1 :]  # include the leading '/'

    if not addr:
        raise click.BadParameter(
            f"Empty address in peer spec: '{spec}'",
            param_hint="PEER_SPEC",
        )
    if not path or path == "/":
        raise click.BadParameter(
            f"Empty or root path in peer spec: '{spec}'",
            param_hint="PEER_SPEC",
        )

    return addr, path
