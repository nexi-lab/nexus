# ruff: noqa: F821
"""
PyInstaller spec for nexus (profile=cluster)
Minimal deployment: storage + ipc + federation
"""

import os

# Rust extension paths
NEXUS_RAFT_SO = "/Users/bgd/anaconda3/envs/nexus/lib/python3.13/site-packages/_nexus_raft/_nexus_raft.cpython-313-darwin.so"
NEXUS_FAST_SO = "/Users/bgd/anaconda3/envs/nexus/lib/python3.13/site-packages/nexus_fast/nexus_fast.cpython-313-darwin.so"

# Hidden imports for Rust extensions and nexus modules
hiddenimports = [
    # Rust extensions
    "_nexus_raft",
    "_nexus_raft._nexus_raft",
    "nexus_fast",
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
    "alembic",
    "grpc",
    "grpc_google_apis",
    "google.protobuf",
    "google.api",
    "google.api_core",
]

# Exclude heavy modules not needed for cluster profile
excludes = [
    "nexus.bricks.llm",
    "nexus.bricks.pay",
    "nexus.bricks.sandbox",
    "nexus.bricks.workflows",
    "nexus.bricks.search",
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
if os.path.exists(NEXUS_FAST_SO):
    binaries.append((NEXUS_FAST_SO, "nexus_fast"))

a = Analysis(
    ["src/nexus/cli/main.py"],
    pathex=["/Users/bgd/repo/nexus/src"],
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
