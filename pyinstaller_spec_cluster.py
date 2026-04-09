# ruff: noqa: F821
"""
PyInstaller spec for nexus (profile=cluster)
Minimal deployment: storage + ipc + federation
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# CI rewrites these placeholders to the actual site-packages paths resolved
# from the runner's Python environment before invoking PyInstaller.
# Rust extension paths
NEXUS_RAFT_SO = "__CI_PATCH_REQUIRED__"
NEXUS_KERNEL_SO = "__CI_PATCH_REQUIRED__"

# Force-collect packages that are imported dynamically or through SQLAlchemy
# dialect lookup so they survive PyInstaller analysis.
search_datas, search_pkg_binaries, search_hiddenimports = collect_all("nexus.bricks.search")
aiosqlite_datas, aiosqlite_binaries, aiosqlite_hiddenimports = collect_all("aiosqlite")
sqlite_datas, sqlite_binaries, sqlite_hiddenimports = collect_all("sqlalchemy.dialects.sqlite")


def _dedupe(items):
    return list(dict.fromkeys(items))


# Hidden imports for Rust extensions and nexus modules
hiddenimports = _dedupe(
    [
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
    ]
    + collect_submodules("nexus.bricks.search")
    + search_hiddenimports
    + aiosqlite_hiddenimports
    + sqlite_hiddenimports
)

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

binaries = _dedupe(search_pkg_binaries + aiosqlite_binaries + sqlite_binaries)
if os.path.exists(NEXUS_RAFT_SO):
    binaries.append((NEXUS_RAFT_SO, "_nexus_raft"))
if os.path.exists(NEXUS_KERNEL_SO):
    binaries.append((NEXUS_KERNEL_SO, "nexus_kernel"))

datas = _dedupe(search_datas + aiosqlite_datas + sqlite_datas)

a = Analysis(
    ["__CI_ENTRYPOINT_PATCH_REQUIRED__"],
    pathex=["__CI_PATHEX_PATCH_REQUIRED__"],
    binaries=binaries,
    datas=datas,
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
