"""`nexus extensions` — cross-kind introspection over the unified manifest layer.

Reads from the in-process manifest store (no impl imports). Pairs with the
existing per-kind commands (`nexus connectors`, `nexus plugins`).
"""

from __future__ import annotations

import json
from typing import Any, cast

import click

from nexus.extensions.types import Kind


def _format_status(report: Any) -> str:
    if report.profile_gate_disabled:
        return "disabled"
    if report.available:
        return "available"
    # Distinguish "we tried and dependencies failed" from "we couldn't
    # verify because the manifest is partial". Otherwise legacy adapters
    # surface as generic missing-deps with empty missing_* lists.
    if report.metadata_incomplete:
        return "unverified"
    return "missing-deps"


def _row(manifest: Any, report: Any) -> dict[str, str]:
    deps = ", ".join(d.name for d in manifest.runtime_deps) or "—"
    return {
        "kind": manifest.kind,
        "name": manifest.name,
        "status": _format_status(report),
        "profile": manifest.profile_gate or "—",
        "deps": deps,
    }


def _print_table(rows: list[dict[str, str]]) -> None:
    cols = ("kind", "name", "status", "profile", "deps")
    widths = {c: max(len(c), max((len(r[c]) for r in rows), default=0)) for c in cols}
    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    click.echo(header)
    for r in rows:
        click.echo("  ".join(r[c].ljust(widths[c]) for c in cols))


def _emit(payload: Any, fmt: str) -> None:
    if fmt == "json":
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif fmt == "yaml":
        try:
            import yaml
        except ImportError:
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        click.echo(yaml.safe_dump(payload, sort_keys=True))
    else:
        raise ValueError(f"Unsupported format: {fmt}")


@click.group(name="extensions")
def extensions() -> None:
    """Inspect plugins, connectors, and bricks via the unified manifest layer."""


@extensions.command("list")
@click.option(
    "--kind",
    type=click.Choice(["connector", "brick", "plugin"]),
    default=None,
    help="Filter by extension kind.",
)
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    help="Filter by profile gate; repeatable.",
)
@click.option(
    "--available-only",
    is_flag=True,
    default=False,
    help="Hide entries with missing runtime deps.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "yaml"]),
    default="table",
)
def list_cmd(
    kind: str | None,
    profiles: tuple[str, ...],
    available_only: bool,
    fmt: str,
) -> None:
    """List registered extensions."""
    from nexus.extensions.introspect import list_extensions
    from nexus.extensions.store import get_store

    profile_set = frozenset(profiles) if profiles else None
    typed_kind = cast(Kind, kind) if kind else None
    manifests = list_extensions(kind=typed_kind, profile=profile_set, available_only=available_only)

    if fmt == "table":
        store = get_store()
        rows = [_row(m, store.check(m)) for m in manifests]
        _print_table(rows)
        return

    payload = [m.model_dump(mode="json") for m in manifests]
    _emit(payload, fmt)


@extensions.command("info")
@click.argument("name")
@click.option(
    "--kind",
    type=click.Choice(["connector", "brick", "plugin"]),
    default=None,
    help="Disambiguate when name collides across kinds.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
)
def info_cmd(name: str, kind: str | None, fmt: str) -> None:
    """Show full manifest for one extension."""
    from nexus.extensions.introspect import get_extension, list_extensions

    if kind is None:
        candidates = [m for m in list_extensions() if m.name == name]
        if not candidates:
            raise click.ClickException(f"No extension named '{name}'")
        if len(candidates) > 1:
            kinds = sorted(m.kind for m in candidates)
            raise click.ClickException(
                f"Name '{name}' is ambiguous across kinds {kinds}; pass --kind"
            )
        manifest = candidates[0]
    else:
        try:
            manifest = get_extension(name, kind=cast(Kind, kind))
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

    _emit(manifest.model_dump(mode="json"), fmt)


@extensions.command("check")
@click.argument("name")
@click.option(
    "--kind",
    type=click.Choice(["connector", "brick", "plugin"]),
    default=None,
)
def check_cmd(name: str, kind: str | None) -> None:
    """Run dependency probes for one extension and print a CheckReport."""
    from nexus.extensions.introspect import check_extension, list_extensions

    if kind is None:
        candidates = [m for m in list_extensions() if m.name == name]
        if not candidates:
            raise click.ClickException(f"No extension named '{name}'")
        if len(candidates) > 1:
            kinds = sorted(m.kind for m in candidates)
            raise click.ClickException(
                f"Name '{name}' is ambiguous across kinds {kinds}; pass --kind"
            )
        kind = candidates[0].kind

    try:
        report = check_extension(name, kind=cast(Kind, kind))
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "name": name,
        "kind": kind,
        "available": report.available,
        "missing_python_deps": list(report.missing_python_deps),
        "missing_binary_deps": list(report.missing_binary_deps),
        "missing_services": list(report.missing_services),
        "import_probe_failures": list(report.import_probe_failures),
        "profile_gate_disabled": report.profile_gate_disabled,
        "metadata_incomplete": report.metadata_incomplete,
    }
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@extensions.command("kinds")
def kinds_cmd() -> None:
    """List the registered extension kinds."""
    from nexus.extensions.introspect import list_kinds

    for k in list_kinds():
        click.echo(k)
