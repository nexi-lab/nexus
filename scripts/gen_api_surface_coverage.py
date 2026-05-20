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
    extract_bricks,
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

_KNOWN_BRICK_IDS = {m.id for m in TAXONOMY_MODULES if m.layer == "brick"}


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
                # 2-token CLI like "nexus hub" / "nexus zone" / "nexus daemon".
                # For deployment-topology verbs, use "verb.cli" so the classifier
                # routes them to the correct deployment module (classify_op_id("hub.cli")
                # returns "hub" because "hub" is in _MODULES_BY_ID as a known module id).
                # For all other bare verbs, keep the old behavior (no suffix) so
                # _upsert's "module.verb" prefixing stays stable.
                tokens = raw.name.split()
                if len(tokens) == 2 and tokens[0] == "nexus":
                    # Normalize hyphens to underscores so "nexus write-batch"
                    # becomes "write_batch" (canonical), matching normalize_cli's
                    # multi-token behavior.
                    verb = tokens[1].replace("-", "_")
                    candidate = classify_op_id(f"{verb}.cli")
                    # verb IS a known module id → use "verb.cli" so classifier routes it;
                    # otherwise keep bare verb so _upsert's "module.verb" prefixing works.
                    op_id = f"{verb}.cli" if candidate == verb else verb
                else:
                    continue
            _upsert(operations, op_id, "cli", raw.name, raw.source)

    # --- HTTP (v3: recursive scan of server/api/ + server/ subdirs) ---
    # Also include server/ for routes in auth/, health/, middleware/ subdirs.
    _http_scanned: set[Path] = set()
    for http_root in (
        repo_root / "src/nexus/server/api",
        repo_root / "src/nexus/server/auth",
        repo_root / "src/nexus/server/health",
    ):
        if not http_root.exists():
            continue
        for raw in extract_http.extract_http_routes(http_root):
            try:
                op_id = normalize.normalize_http(raw.method, raw.path)
            except ValueError:
                # Relative path (no /api/v<N>/ prefix) — infer module from the
                # router file stem (e.g. "rebac.py" -> "rebac").
                source_file = Path(raw.source.split(":")[0])
                stem = source_file.stem.replace("-", "_")
                # Collapse path segments into a verb string.
                path_parts = [p for p in raw.path.strip("/").split("/") if p]
                if not path_parts:
                    verb = raw.method.lower()
                else:
                    verb = "_".join(p.strip("{}").replace("-", "_") for p in path_parts[:2])
                op_id = f"{stem}.{verb}"
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

    # --- gRPC typed (recursive scan of proto/) ---
    proto_root = repo_root / "proto"
    if proto_root.exists():
        for proto_file in sorted(proto_root.rglob("*.proto")):
            for raw in extract_grpc_typed.extract_grpc_typed_methods(proto_file):
                try:
                    op_id = normalize.normalize_grpc_typed(raw.method)
                except ValueError:
                    # Service.Method shape but unknown service → keep "<svc>.<method>"
                    # lowercased so classify_op_id can route via substring rules.
                    service, _, method_name = raw.method.partition(".")
                    op_id = f"{service.lower()}.{method_name.lower()}"
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

    # --- Bricks (metadata-only; each brick becomes a module via taxonomy) ---
    bricks_root = repo_root / "src/nexus/bricks"
    if bricks_root.exists():
        for raw in extract_bricks.extract_bricks(bricks_root):
            # Each brick is represented as an Operation for visibility in YAML even
            # without its own external surfaces. Op-id "brick.<name>".
            op_id = f"{raw.id}.brick"
            _upsert(operations, op_id, "grpc_expose", f"brick:{raw.id}", raw.source)
            # Override module to match brick id (classifier may have placed it elsewhere)
            operations[op_id].module = raw.id if raw.id in _KNOWN_BRICK_IDS else "uncategorized"
            # Stash brick metadata in summary if available
            if raw.brick_name:
                operations[
                    op_id
                ].summary = f"brick gate: {raw.brick_name}, tier: {raw.tier or 'n/a'}"

    # --- SDK (v3: walk the whole remote/ tree) ---
    remote_root = repo_root / "src/nexus/remote"
    if remote_root.exists():
        for raw in extract_sdk.extract_sdk_methods(remote_root):
            try:
                op_id = normalize.normalize_sdk(raw.class_name, raw.method_name)
            except ValueError:
                continue
            _upsert(operations, op_id, "sdk", f"{raw.class_name}.{raw.method_name}", raw.source)

    # --- Profiles (extracted but not directly mapped to operations in v1) ---
    dp = repo_root / "src/nexus/contracts/deployment_profile.py"
    if dp.exists():
        _ = extract_profiles.extract_profile_names(dp, enum_class="DeploymentProfile")

    # --- Manual missing_needed gaps from api-rpc-surface-gaps.yaml ---
    gaps_path = repo_root / "docs/surface-coverage/api-rpc-surface-gaps.yaml"
    if gaps_path.exists():
        import yaml as _yaml

        gaps_doc = _yaml.safe_load(gaps_path.read_text(encoding="utf-8")) or {}
        for gap in gaps_doc.get("missing_operations", []):
            op_id = gap["id"]
            if op_id in operations:
                # Real surface exists with same id; don't overwrite — log via summary
                continue
            operations[op_id] = Operation(
                id=op_id,
                module=gap.get("module") or classify_op_id(op_id),
                summary=gap.get("summary", ""),
                transports={},  # no transport - intentionally missing
                profiles={},  # set below to all missing_needed
                gap_issue=gap.get("gap_issue"),
                owning_issue=gap.get("owning_issue"),
            )
            # Mark all three profiles as missing_needed
            operations[op_id].profiles = dict.fromkeys(
                ("lite", "sandbox", "full"), ProfileStatus.MISSING_NEEDED
            )
            # Stash wanted_why in usage_example so it surfaces somewhere visible
            wanted = gap.get("wanted_why", "")
            if wanted:
                operations[op_id].usage_example = f"WANTED: {wanted}"

    # Default profile assignment: extractor marks everything supported on all three.
    # Subissues override to unavailable/admin_only/etc.
    # Skip ops that already have profiles set (e.g. missing_needed gaps above).
    default_profiles = dict.fromkeys(("lite", "sandbox", "full"), ProfileStatus.SUPPORTED)
    for op in operations.values():
        if not op.profiles:
            op.profiles = dict(default_profiles)

    # Rewrite transport sources to repo-relative paths so committed YAML
    # doesn't leak the local workspace path.
    repo_root_str = str(repo_root.resolve()) + "/"
    for op in operations.values():
        rewritten: dict[str, TransportCell] = {}
        for tkey, cell in op.transports.items():
            src = cell.source
            if src.startswith(repo_root_str):
                src = src[len(repo_root_str) :]
            rewritten[tkey] = TransportCell(name=cell.name, source=src)
        op.transports = rewritten

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
                    id=m.id,
                    name=m.name,
                    description=m.description,
                    layer=m.layer,
                    depends_on=list(m.depends_on),
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

    # Rewrite any stale absolute paths preserved from prior YAML so committed
    # output is repo-relative regardless of where it was generated.
    repo_root_str = str(repo_root.resolve()) + "/"
    for op in merged.operations:
        rewritten: dict[str, TransportCell] = {}
        for tkey, cell in op.transports.items():
            src = cell.source
            if src.startswith(repo_root_str):
                src = src[len(repo_root_str) :]
            rewritten[tkey] = TransportCell(name=cell.name, source=src)
        op.transports = rewritten

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
        default=Path("docs/surface-coverage/api-rpc-surface-coverage.yaml"),
    )
    p.add_argument(
        "--overrides",
        type=Path,
        default=Path("docs/surface-coverage/api-rpc-surface-overrides.yaml"),
        help="reserved for v2; ignored in v1",
    )
    args = p.parse_args(argv)
    generate_coverage(repo_root=args.repo_root, output=args.output, overrides=args.overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
