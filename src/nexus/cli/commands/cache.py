"""Cache management commands - warmup, stats, clear (Issue #1076)."""

import asyncio
from typing import Any, cast

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    console,
    handle_error,
    open_filesystem,
)
from nexus.contracts.constants import ROOT_ZONE_ID


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
@click.option("-z", "--zone-id", type=str, default=ROOT_ZONE_ID, help="Zone ID")
@add_output_options
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
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
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
    timing = CommandTiming()

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            # Import here to avoid circular imports
            from nexus.server.cache_warmer import (
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
                nexus_fs=cast(Any, nx),
                config=config,
                file_tracker=file_tracker,
            )

            # Run warmup
            async def run_warmup() -> dict[str, Any]:
                if user:
                    # History-based warmup
                    if not output_opts.quiet and not output_opts.json_output:
                        console.print(
                            f"[nexus.reference]Warming cache based on {user}'s access history...[/nexus.reference]"
                        )
                    warmup_stats = await warmer.warmup_from_history(
                        user=user,
                        hours=hours,
                        max_files=max_files,
                        zone_id=zone_id,
                    )
                else:
                    # Directory-based warmup
                    if not output_opts.quiet and not output_opts.json_output:
                        console.print(
                            f"[nexus.reference]Warming cache for {path}...[/nexus.reference]"
                        )
                    warmup_stats = await warmer.warmup_directory(
                        path=path,
                        depth=depth,
                        include_content=include_content and not metadata_only,
                        max_files=max_files,
                        zone_id=zone_id,
                    )
                return warmup_stats.to_dict()

            with timing.phase("server"):
                warmup_data = await run_warmup()

            def _render(data: dict[str, Any]) -> None:
                console.print("\n[nexus.success]Cache warmup complete![/nexus.success]")
                console.print(f"  Files warmed: {data['files_warmed']}")
                console.print(f"  Metadata warmed: {data['metadata_warmed']}")
                console.print(f"  Content warmed: {data['content_warmed']}")
                console.print(f"  Bytes warmed: {data['bytes_warmed_mb']} MB")
                console.print(f"  Duration: {data['duration_seconds']}s")
                if data["errors"] > 0:
                    console.print(f"  [nexus.warning]Errors: {data['errors']}[/nexus.warning]")
                if data["skipped"] > 0:
                    console.print(f"  Skipped: {data['skipped']}")

            render_output(
                data=warmup_data,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render,
            )

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@cache_group.command(name="stats")
@add_output_options
@add_backend_options
def stats(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show cache statistics.

    Displays hit rates, memory usage, and entry counts for all cache layers.

    Examples:
        nexus cache stats
        nexus cache stats --json
    """
    timing = CommandTiming()

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            with timing.phase("server"):
                # Collect stats from various cache layers
                cache_stats: dict[str, Any] = {}

                # Permission cache stats
                rm = nx.service("rebac_manager") if hasattr(nx, "service") else None
                if rm is not None:
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
                from nexus.server.cache_warmer import get_file_access_tracker

                tracker = get_file_access_tracker()
                cache_stats["file_access_tracker"] = tracker.get_stats()

            def _render(data: dict[str, Any]) -> None:
                console.print("\n[bold]Cache Statistics[/bold]")
                console.print("=" * 40)

                for cache_name, stats_data in data.items():
                    console.print(f"\n[nexus.value]{cache_name}:[/nexus.value]")
                    if isinstance(stats_data, dict):
                        for key, value in stats_data.items():
                            if isinstance(value, float):
                                console.print(f"  {key}: {value:.4f}")
                            else:
                                console.print(f"  {key}: {value}")
                    else:
                        console.print(f"  {stats_data}")

            render_output(
                data=cache_stats,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render,
            )

    try:
        asyncio.run(_impl())
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
    remote_url: str | None,
    remote_api_key: str | None,
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
            "[nexus.warning]Specify which cache to clear: --metadata, --content, --permissions, or --all[/nexus.warning]"
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
            console.print("[nexus.warning]Cancelled[/nexus.warning]")
            return

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            cleared: list[str] = []

            # Clear permission cache
            if permissions or clear_all:
                rm = nx.service("rebac_manager") if hasattr(nx, "service") else None  # Issue #1771
                if rm is not None:
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
                from nexus.server.cache_warmer import get_file_access_tracker

                tracker = get_file_access_tracker()
                tracker.clear()
                cleared.append("file_access_tracker")

            if cleared:
                console.print(f"[nexus.success]Cleared: {', '.join(cleared)}[/nexus.success]")
            else:
                console.print("[nexus.warning]No caches were cleared[/nexus.warning]")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@cache_group.command(name="hot")
@click.option("-n", "--limit", type=int, default=20, help="Number of hot files to show")
@click.option("-z", "--zone-id", type=str, default=ROOT_ZONE_ID, help="Zone ID")
@click.option("-u", "--user", type=str, help="Filter by user")
@add_output_options
def hot(
    limit: int,
    zone_id: str,
    user: str | None,
    output_opts: OutputOptions,
) -> None:
    """Show hot (frequently accessed) files.

    Displays files that are accessed frequently, useful for understanding
    access patterns and cache effectiveness.

    Examples:
        nexus cache hot
        nexus cache hot --limit 50
        nexus cache hot --user alice
        nexus cache hot --json
    """
    timing = CommandTiming()
    try:
        from nexus.server.cache_warmer import get_file_access_tracker

        with timing.phase("collect"):
            tracker = get_file_access_tracker()
            hot_files = tracker.get_hot_files(
                zone_id=zone_id,
                user_id=user,
                limit=limit,
            )

        hot_data = [{"path": entry.path, "access_count": entry.access_count} for entry in hot_files]

        def _render(data: list[dict[str, Any]]) -> None:
            if not data:
                console.print("[nexus.warning]No hot files detected yet.[/nexus.warning]")
                console.print("Hot files are tracked as the filesystem is used.")
                return

            console.print(f"\n[bold]Hot Files (top {limit})[/bold]")
            console.print("=" * 60)

            for i, entry in enumerate(data, 1):
                console.print(
                    f"{i:3}. [nexus.path]{entry['path']}[/nexus.path] ({entry['access_count']} accesses)"
                )

        render_output(
            data=hot_data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        handle_error(e)
