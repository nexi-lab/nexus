"""Cache management commands - warmup, stats, clear (Issue #1076)."""

from __future__ import annotations

import asyncio
from typing import Any

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register all cache management commands."""
    cli.add_command(cache_group)


@click.group(name="cache")
def cache_group() -> None:
    """Cache management commands.

    Manage cache warming, statistics, and clearing.
    """
    pass


@cache_group.command(name="warmup")
@click.argument("path", type=str, default="/", required=False)
@click.option("-d", "--depth", type=int, default=2, help="Directory depth to warm (default: 2)")
@click.option(
    "-c", "--include-content", is_flag=True, help="Also warm file content (not just metadata)"
)
@click.option(
    "-m", "--max-files", type=int, default=1000, help="Maximum files to warm (default: 1000)"
)
@click.option("--metadata-only", is_flag=True, help="Only warm metadata (faster, less memory)")
@click.option("-u", "--user", type=str, help="Warm cache based on user's access history")
@click.option(
    "--hours", type=int, default=24, help="Look back N hours for history-based warmup (default: 24)"
)
@click.option("-z", "--zone-id", type=str, default="default", help="Zone ID")
@add_backend_options
def warmup(
    path: str,
    depth: int,
    include_content: bool,
    max_files: int,
    metadata_only: bool,
    user: str | None,
    hours: int,
    zone_id: str,
    backend_config: BackendConfig,
) -> None:
    """Pre-populate cache for faster access.

    Reduces cold-start latency by pre-caching frequently accessed files.

    Examples:

        # Warm up a directory (metadata only)
        nexus cache warmup /workspace/project

        # Warm up with content (slower, but faster subsequent reads)
        nexus cache warmup /workspace/project --include-content

        # Warm up deeper directory tree
        nexus cache warmup /workspace --depth 3

        # Warm up based on user's recent access patterns
        nexus cache warmup --user alice --hours 24

        # Warm up for FUSE mount (metadata only, many files)
        nexus cache warmup /workspace --metadata-only --max-files 10000
    """
    try:
        nx = get_filesystem(backend_config)

        # Import here to avoid circular imports
        from nexus.cache.warmer import (
            CacheWarmer,
            WarmupConfig,
            get_file_access_tracker,
        )

        # Create config
        config = WarmupConfig(
            max_files=max_files,
            depth=depth,
            include_content=include_content and not metadata_only,
        )

        # Get or create file tracker for history-based warmup
        file_tracker = get_file_access_tracker() if user else None

        # Create warmer
        warmer = CacheWarmer(
            nexus_fs=nx,  # type: ignore[arg-type]
            config=config,
            file_tracker=file_tracker,
        )

        # Run warmup
        async def run_warmup() -> dict[str, Any]:
            if user:
                # History-based warmup
                console.print(f"[blue]Warming cache based on {user}'s access history...[/blue]")
                stats = await warmer.warmup_from_history(
                    user=user,
                    hours=hours,
                    max_files=max_files,
                    zone_id=zone_id,
                )
            else:
                # Directory-based warmup
                console.print(f"[blue]Warming cache for {path}...[/blue]")
                stats = await warmer.warmup_directory(
                    path=path,
                    depth=depth,
                    include_content=include_content and not metadata_only,
                    max_files=max_files,
                    zone_id=zone_id,
                )
            return stats.to_dict()

        stats = asyncio.run(run_warmup())

        # Display results
        console.print("\n[green]Cache warmup complete![/green]")
        console.print(f"  Files warmed: {stats['files_warmed']}")
        console.print(f"  Metadata warmed: {stats['metadata_warmed']}")
        console.print(f"  Content warmed: {stats['content_warmed']}")
        console.print(f"  Bytes warmed: {stats['bytes_warmed_mb']} MB")
        console.print(f"  Duration: {stats['duration_seconds']}s")
        if stats["errors"] > 0:
            console.print(f"  [yellow]Errors: {stats['errors']}[/yellow]")
        if stats["skipped"] > 0:
            console.print(f"  Skipped: {stats['skipped']}")

        nx.close()

    except Exception as e:
        handle_error(e)


@cache_group.command(name="stats")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@add_backend_options
def stats(
    as_json: bool,
    backend_config: BackendConfig,
) -> None:
    """Show cache statistics.

    Displays hit rates, memory usage, and entry counts for all cache layers.

    Examples:
        nexus cache stats
        nexus cache stats --json
    """
    try:
        nx = get_filesystem(backend_config)

        # Collect stats from various cache layers
        cache_stats: dict[str, Any] = {}

        # Metadata cache stats
        if hasattr(nx, "metadata") and hasattr(nx.metadata, "_cache"):
            cache = nx.metadata._cache
            if cache:
                cache_stats["metadata_cache"] = {
                    "path_cache_size": len(getattr(cache, "_path_cache", {})),
                    "list_cache_size": len(getattr(cache, "_list_cache", {})),
                    "exists_cache_size": len(getattr(cache, "_exists_cache", {})),
                }

        # Content cache stats
        if hasattr(nx, "backend") and hasattr(nx.backend, "content_cache"):
            cc = nx.backend.content_cache
            if cc and hasattr(cc, "get_stats"):
                cache_stats["content_cache"] = cc.get_stats()

        # Permission cache stats
        if hasattr(nx, "_rebac_manager"):
            rm = nx._rebac_manager
            if hasattr(rm, "_permission_cache") and rm._permission_cache:
                pc = rm._permission_cache
                if hasattr(pc, "get_stats"):
                    cache_stats["permission_cache"] = pc.get_stats()

            # Tiger cache stats
            if hasattr(rm, "_tiger_cache") and rm._tiger_cache:
                tc = rm._tiger_cache
                if hasattr(tc, "get_stats"):
                    cache_stats["tiger_cache"] = tc.get_stats()

        # Directory visibility cache
        if hasattr(nx, "_dir_visibility_cache") and nx._dir_visibility_cache:
            dvc = nx._dir_visibility_cache
            if hasattr(dvc, "get_metrics"):
                cache_stats["dir_visibility_cache"] = dvc.get_metrics()

        # File access tracker stats
        from nexus.cache.warmer import get_file_access_tracker

        tracker = get_file_access_tracker()
        cache_stats["file_access_tracker"] = tracker.get_stats()

        if as_json:
            import json

            console.print(json.dumps(cache_stats, indent=2, default=str))
        else:
            console.print("\n[bold]Cache Statistics[/bold]")
            console.print("=" * 40)

            for cache_name, stats_data in cache_stats.items():
                console.print(f"\n[cyan]{cache_name}:[/cyan]")
                if isinstance(stats_data, dict):
                    for key, value in stats_data.items():
                        if isinstance(value, float):
                            console.print(f"  {key}: {value:.4f}")
                        else:
                            console.print(f"  {key}: {value}")
                else:
                    console.print(f"  {stats_data}")

        nx.close()

    except Exception as e:
        handle_error(e)


@cache_group.command(name="clear")
@click.option("--metadata", is_flag=True, help="Clear metadata cache")
@click.option("--content", is_flag=True, help="Clear content cache")
@click.option("--permissions", is_flag=True, help="Clear permission cache")
@click.option("--all", "clear_all", is_flag=True, help="Clear all caches")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@add_backend_options
def clear(
    metadata: bool,
    content: bool,
    permissions: bool,
    clear_all: bool,
    yes: bool,
    backend_config: BackendConfig,
) -> None:
    """Clear cache entries.

    WARNING: This can temporarily degrade performance until caches are rebuilt.

    Examples:
        nexus cache clear --all
        nexus cache clear --metadata
        nexus cache clear --content --permissions
    """
    if not any([metadata, content, permissions, clear_all]):
        console.print(
            "[yellow]Specify which cache to clear: --metadata, --content, --permissions, or --all[/yellow]"
        )
        return

    if not yes:
        if clear_all:
            msg = "Clear ALL caches?"
        else:
            caches = []
            if metadata:
                caches.append("metadata")
            if content:
                caches.append("content")
            if permissions:
                caches.append("permissions")
            msg = f"Clear {', '.join(caches)} cache(s)?"

        if not click.confirm(msg):
            console.print("[yellow]Cancelled[/yellow]")
            return

    try:
        nx = get_filesystem(backend_config)
        cleared: list[str] = []

        # Clear metadata cache
        if (metadata or clear_all) and hasattr(nx, "metadata") and hasattr(nx.metadata, "_cache"):
            cache = nx.metadata._cache
            if cache:
                if hasattr(cache, "_path_cache"):
                    cache._path_cache.clear()
                if hasattr(cache, "_list_cache"):
                    cache._list_cache.clear()
                if hasattr(cache, "_exists_cache"):
                    cache._exists_cache.clear()
                cleared.append("metadata")

        # Clear content cache
        if (
            (content or clear_all)
            and hasattr(nx, "backend")
            and hasattr(nx.backend, "content_cache")
        ):
            cc = nx.backend.content_cache
            if cc and hasattr(cc, "clear"):
                cc.clear()
                cleared.append("content")

        # Clear permission cache
        if permissions or clear_all:
            if hasattr(nx, "_rebac_manager"):
                rm = nx._rebac_manager
                if hasattr(rm, "_permission_cache") and rm._permission_cache:
                    pc = rm._permission_cache
                    if hasattr(pc, "clear"):
                        pc.clear()
                    cleared.append("permission")

                # Also clear tiger cache
                if hasattr(rm, "_tiger_cache") and rm._tiger_cache:
                    tc = rm._tiger_cache
                    if hasattr(tc, "invalidate_all"):
                        tc.invalidate_all()
                    cleared.append("tiger")

            # Clear directory visibility cache
            if hasattr(nx, "_dir_visibility_cache") and nx._dir_visibility_cache:
                dvc = nx._dir_visibility_cache
                if hasattr(dvc, "clear"):
                    dvc.clear()
                cleared.append("dir_visibility")

        # Clear file access tracker
        if clear_all:
            from nexus.cache.warmer import get_file_access_tracker

            tracker = get_file_access_tracker()
            tracker.clear()
            cleared.append("file_access_tracker")

        if cleared:
            console.print(f"[green]Cleared: {', '.join(cleared)}[/green]")
        else:
            console.print("[yellow]No caches were cleared[/yellow]")

        nx.close()

    except Exception as e:
        handle_error(e)


@cache_group.command(name="hot")
@click.option("-n", "--limit", type=int, default=20, help="Number of hot files to show")
@click.option("-z", "--zone-id", type=str, default="default", help="Zone ID")
@click.option("-u", "--user", type=str, help="Filter by user")
def hot(
    limit: int,
    zone_id: str,
    user: str | None,
) -> None:
    """Show hot (frequently accessed) files.

    Displays files that are accessed frequently, useful for understanding
    access patterns and cache effectiveness.

    Examples:
        nexus cache hot
        nexus cache hot --limit 50
        nexus cache hot --user alice
    """
    try:
        from nexus.cache.warmer import get_file_access_tracker

        tracker = get_file_access_tracker()
        hot_files = tracker.get_hot_files(
            zone_id=zone_id,
            user_id=user,
            limit=limit,
        )

        if not hot_files:
            console.print("[yellow]No hot files detected yet.[/yellow]")
            console.print("Hot files are tracked as the filesystem is used.")
            return

        console.print(f"\n[bold]Hot Files (top {limit})[/bold]")
        console.print("=" * 60)

        for i, entry in enumerate(hot_files, 1):
            console.print(f"{i:3}. [cyan]{entry.path}[/cyan] ({entry.access_count} accesses)")

    except Exception as e:
        handle_error(e)
