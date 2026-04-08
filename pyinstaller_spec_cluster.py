# ruff: noqa: F821
"""
PyInstaller spec for nexus (profile=cluster)
Minimal deployment: storage + ipc + federation
"""

import os
from PyInstaller.utils.hooks import collect_submodules

# CI rewrites these placeholders to the actual site-packages paths resolved
# from the runner's Python environment before invoking PyInstaller.
# Rust extension paths
NEXUS_RAFT_SO = "__CI_PATCH_REQUIRED__"
NEXUS_KERNEL_SO = "__CI_PATCH_REQUIRED__"

# Hidden imports for Rust extensions and nexus modules
hiddenimports = [
    # Rust extensions
    "_nexus_raft",
    "_nexus_raft._nexus_raft",
    "nexus_kernel",
    # Cluster profile core modules
    "nexus",
    "nexus.cli",
    "nexus.cli.main",
    "nexus.daemon",
    "nexus.daemon.main",
    "nexus.raft",
    "nexus.raft.federation",
    "nexus.raft.zone_manager",
    "nexus.storage",
    "nexus.storage.raft_metadata_store",
    "nexus.storage.dict_metastore",
    "nexus.bricks.search",
    "nexus.bricks.search.search_service",
    "nexus.bricks.search.primitives",
    "nexus.bricks.ipc",
    "nexus.bricks.federation",
    "nexus.contracts.deployment_profile",
    # Required dependencies
    "click",
    "rich",
    "tqdm",
    "pydantic",
    "pyyaml",
    "httpx",
    "requests",
    "uvicorn",
    "fastapi",
    "starlette",
    "sqlalchemy",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "aiosqlite",
    "alembic",
    "grpc",
    "grpc_google_apis",
    "google.protobuf",
    "google.api",
    "google.api_core",
] + collect_submodules("nexus.bricks.search")

# Exclude heavy modules not needed for cluster profile
excludes = [
    "nexus.bricks.llm",
    "nexus.bricks.pay",
    "nexus.bricks.sandbox",
    "nexus.bricks.workflows",
    "nexus.bricks.memory",
    "nexus.bricks.mcp",
    "nexus.bricks.agent_runtime",
    "nexus.bricks.scheduler",
    "nexus.bricks.cache",
    "nexus.bricks.observability",
    "nexus.bricks.uploads",
    "nexus.bricks.resiliency",
    "nexus.bricks.acp",
    "nexus.bricks.permissions",
    "nexus.bricks.namespace",
    "nexus.bricks.eventlog",
]

binaries = []
if os.path.exists(NEXUS_RAFT_SO):
    binaries.append((NEXUS_RAFT_SO, "_nexus_raft"))
if os.path.exists(NEXUS_KERNEL_SO):
    binaries.append((NEXUS_KERNEL_SO, "nexus_kernel"))

a = Analysis(
    ["__CI_ENTRYPOINT_PATCH_REQUIRED__"],
    pathex=["__CI_PATHEX_PATCH_REQUIRED__"],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="nexus-cluster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
