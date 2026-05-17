#!/usr/bin/env python3
"""Orchestrator: run every surface extractor against the repo and emit YAML.

Reads the existing YAML (if present) and merges, preserving human-filled fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `from scripts...` imports when running this file directly via
# `uv run python scripts/gen_api_surface_coverage.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse  # noqa: E402

from scripts.surface_coverage import (  # noqa: E402
    extract_cli,
    extract_grpc_call,
    extract_grpc_typed,
    extract_http,
    extract_mcp,
    extract_profiles,
    extract_rpc_expose,
    extract_sdk,
    normalize,
)
from scripts.surface_coverage.merge import merge_coverage  # noqa: E402
from scripts.surface_coverage.schema import (  # noqa: E402
    Module,
    Operation,
    ParityWarning,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
    dump_yaml,
    load_yaml,
)
from scripts.surface_coverage.taxonomy import (
    MODULES as TAXONOMY_MODULES,
)
from scripts.surface_coverage.taxonomy import (  # noqa: E402
    classify_op_id,
)


def generate_coverage(
    *,
    repo_root: Path,
    output: Path,
    overrides: Path | None,
) -> SurfaceCoverage:
    operations: dict[str, Operation] = {}

    # --- CLI ---
    cli_init = repo_root / "src/nexus/cli/commands/__init__.py"
    if cli_init.exists():
        for raw in extract_cli.extract_cli_commands(cli_init):
            try:
                op_id = normalize.normalize_cli(raw.name)
            except ValueError:
                # Two-token form "nexus <verb>" — classifier handles flat names.
                parts = raw.name.strip().split()
                if len(parts) == 2 and parts[0] == "nexus":
                    op_id = parts[1]  # classifier handles flat names
                else:
                    continue
            _upsert(operations, op_id, "cli", raw.name, raw.source)

    # --- HTTP ---
    fastapi = repo_root / "src/nexus/server/fastapi_server.py"
    if fastapi.exists():
        for raw in extract_http.extract_http_routes(fastapi):
            try:
                op_id = normalize.normalize_http(raw.method, raw.path)
            except ValueError:
                continue
            _upsert(operations, op_id, "http", f"{raw.method} {raw.path}", raw.source)

    # --- MCP ---
    tp = repo_root / "src/nexus/config/tool_profiles.yaml"
    if tp.exists():
        for raw in extract_mcp.extract_mcp_tools(tp):
            try:
                op_id = normalize.normalize_mcp(raw.name)
            except ValueError:
                continue
            _upsert(operations, op_id, "mcp", raw.name, raw.source)

    # --- gRPC typed ---
    proto = repo_root / "proto/nexus/grpc/vfs/vfs.proto"
    if proto.exists():
        for raw in extract_grpc_typed.extract_grpc_typed_methods(proto):
            try:
                op_id = normalize.normalize_grpc_typed(raw.method)
            except ValueError:
                continue
            _upsert(operations, op_id, "grpc_typed", raw.method, raw.source)

    # --- gRPC Call (frozenset of syscall names) ---
    dispatch = repo_root / "src/nexus/server/_kernel_syscall_dispatch.py"
    if dispatch.exists():
        # Real file uses KERNEL_SYSCALL_NAMES; tolerate alternates if codegen changes.
        for var in (
            "KERNEL_SYSCALL_NAMES",
            "DISPATCH",
            "_DISPATCH",
            "KERNEL_SYSCALL_DISPATCH",
            "SYSCALL_DISPATCH",
        ):
            try:
                names = extract_grpc_call.extract_grpc_call_names(dispatch, dispatch_var=var)
            except ValueError:
                continue
            for raw in names:
                op_id = raw.name  # classifier handles flat names
                _upsert(operations, op_id, "grpc_call", raw.name, raw.source)
            break

    # --- @rpc_expose ---
    src = repo_root / "src/nexus"
    if src.exists():
        for raw in extract_rpc_expose.extract_rpc_exposes(src):
            # rpc_expose names use MCP-like underscore form (oauth_list_providers).
            # Best-effort map via the MCP normalizer; fall back to a kernel.<n> form.
            try:
                op_id = normalize.normalize_mcp("nexus_" + raw.name)
            except ValueError:
                op_id = raw.name  # classifier handles unprefixed names
            _upsert(operations, op_id, "grpc_expose", raw.name, raw.source)

    # --- SDK ---
    bc = repo_root / "src/nexus/remote/base_client.py"
    if bc.exists():
        # Real class is BaseRemoteNexusFS; tolerate variations.
        for raw in extract_sdk.extract_sdk_methods(
            bc, class_names=("BaseRemoteNexusFS", "BaseRemoteClient")
        ):
            try:
                op_id = normalize.normalize_sdk(raw.class_name, raw.method_name)
            except ValueError:
                continue
            _upsert(operations, op_id, "sdk", f"{raw.class_name}.{raw.method_name}", raw.source)

    # --- Profiles (extracted but not directly mapped to operations in v1) ---
    dp = repo_root / "src/nexus/contracts/deployment_profile.py"
    if dp.exists():
        _ = extract_profiles.extract_profile_names(dp, enum_class="DeploymentProfile")

    # Default profile assignment: extractor marks everything supported on all three.
    # Subissues override to unavailable/admin_only/etc.
    default_profiles = dict.fromkeys(("lite", "sandbox", "full"), ProfileStatus.SUPPORTED)
    for op in operations.values():
        op.profiles = dict(default_profiles)

    # Parity warnings: ops exposed via some "user-facing" transports but not all.
    user_facing = {"cli", "grpc_typed", "http", "mcp", "sdk"}
    parity_warnings: list[ParityWarning] = []
    for op in operations.values():
        has = sorted(set(op.transports) & user_facing)
        missing = sorted(user_facing - set(op.transports))
        if has and missing:
            parity_warnings.append(ParityWarning(operation_id=op.id, has=has, missing=missing))

    fresh = SurfaceCoverage(
        schema_version=1,
        modules=sorted(
            [
                # convert CuratedModule -> schema.Module for serialization
                Module(
                    id=m.id, name=m.name, description=m.description, depends_on=list(m.depends_on)
                )
                for m in TAXONOMY_MODULES
            ],
            key=lambda m: m.id,
        ),
        operations=sorted(operations.values(), key=lambda o: o.id),
        parity_warnings=sorted(parity_warnings, key=lambda w: w.operation_id),
        unmapped_surfaces=[],
        stale_rows=[],
    )

    if output.exists():
        existing = load_yaml(output)
        merged = merge_coverage(existing=existing, fresh=fresh)
    else:
        merged = fresh

    dump_yaml(merged, output)
    return merged


def _upsert(
    ops: dict[str, Operation],
    op_id: str,
    transport: str,
    name: str,
    source: str,
) -> None:
    module = classify_op_id(op_id)
    # Ensure canonical "module.verb" form. Bare verb names (no ".") get prefixed
    # with their classified module so op-ids are stable and human-readable.
    if "." not in op_id:
        op_id = f"{module}.{op_id}"
    if op_id not in ops:
        ops[op_id] = Operation(
            id=op_id,
            module=module,
            summary="",
            transports={},
            profiles={},  # filled later
        )
    ops[op_id].transports[transport] = TransportCell(name=name, source=source)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument(
        "--output",
        type=Path,
        default=Path("docs/architecture/api-rpc-surface-coverage.yaml"),
    )
    p.add_argument(
        "--overrides",
        type=Path,
        default=Path("docs/architecture/api-rpc-surface-overrides.yaml"),
        help="reserved for v2; ignored in v1",
    )
    args = p.parse_args(argv)
    generate_coverage(repo_root=args.repo_root, output=args.output, overrides=args.overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
