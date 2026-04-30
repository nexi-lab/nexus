"""Wheel-content audit for the slim package.

Pins the set of nexus.bricks paths that the slim wheel ships. Any
deviation from the expected set fails CI loudly, preventing silent
regressions when pyproject.toml allowlist entries change.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

# Paths that MUST appear in the slim wheel.
# Update this list whenever force-include entries change in
# packages/nexus-fs/pyproject.toml.
REQUIRED_BRICKS_PATHS = [
    # Top-level bricks namespace
    "nexus/bricks/__init__.py",
    # search/primitives subtree
    "nexus/bricks/search/__init__.py",
    "nexus/bricks/search/primitives/__init__.py",
    "nexus/bricks/search/primitives/glob_helpers.py",
    "nexus/bricks/search/primitives/trigram_fast.py",
    # auth: individual files carved in
    "nexus/bricks/auth/__init__.py",
    "nexus/bricks/auth/types.py",
    "nexus/bricks/auth/protocol.py",
    "nexus/bricks/auth/constants.py",
    "nexus/bricks/auth/profile.py",
    "nexus/bricks/auth/credential_backend.py",
    "nexus/bricks/auth/credential_pool.py",
    # auth/oauth subtree (entire directory force-included)
    "nexus/bricks/auth/oauth/__init__.py",
    "nexus/bricks/auth/oauth/base_provider.py",
    "nexus/bricks/auth/oauth/config.py",
    "nexus/bricks/auth/oauth/credential_service.py",
    "nexus/bricks/auth/oauth/crypto.py",
    "nexus/bricks/auth/oauth/factory.py",
    "nexus/bricks/auth/oauth/pending.py",
    "nexus/bricks/auth/oauth/protocol.py",
    # token_manager.py ships in slim so connectors can instantiate with a token DB.
    # sqlalchemy>=2.0 is added to each OAuth connector extra to satisfy the import.
    "nexus/bricks/auth/oauth/token_manager.py",
    "nexus/bricks/auth/oauth/token_resolver.py",
    "nexus/bricks/auth/oauth/types.py",
    # user_auth.py excluded: sqlalchemy + auth/providers.local (server-runtime only)
    # "nexus/bricks/auth/oauth/user_auth.py",
    # auth/classifiers subtree (entire directory force-included)
    "nexus/bricks/auth/classifiers/__init__.py",
    "nexus/bricks/auth/classifiers/anthropic.py",
    "nexus/bricks/auth/classifiers/boto3.py",
    "nexus/bricks/auth/classifiers/google.py",
    "nexus/bricks/auth/classifiers/openai.py",
    "nexus/bricks/auth/classifiers/slack.py",
]

# bricks paths that MUST NOT appear in the slim wheel.
# These are server-only files intentionally omitted from force-include.
FORBIDDEN_BRICKS_PREFIXES = [
    # Server-only auth files (top-level imports of sqlalchemy, security, etc.)
    "nexus/bricks/auth/unified_service.py",
    "nexus/bricks/auth/postgres_profile_store.py",
    "nexus/bricks/auth/postgres_migrate.py",
    "nexus/bricks/auth/migrate.py",
    "nexus/bricks/auth/user_queries.py",
    "nexus/bricks/auth/cache.py",
    "nexus/bricks/auth/consumer_metrics.py",
    # user_auth has top-level sqlalchemy + server-only provider imports — excluded from slim
    "nexus/bricks/auth/oauth/user_auth.py",
    # auth subtrees excluded entirely
    "nexus/bricks/auth/stores/",
    "nexus/bricks/auth/daemon/",
    "nexus/bricks/auth/providers/",
    # test code must not ship in the slim wheel
    "nexus/bricks/auth/oauth/tests/",
    # All other top-level bricks subdirs — never included in slim
    "nexus/bricks/access_manifest/",
    "nexus/bricks/catalog/",
    "nexus/bricks/context_manifest/",
    "nexus/bricks/delegation/",
    "nexus/bricks/discovery/",
    "nexus/bricks/filesystem/",
    "nexus/bricks/governance/",
    "nexus/bricks/identity/",
    "nexus/bricks/mcp/",
    "nexus/bricks/mount/",
    "nexus/bricks/parsers/",
    "nexus/bricks/pay/",
    "nexus/bricks/portability/",
    "nexus/bricks/rebac/",
    "nexus/bricks/sandbox/",
    "nexus/bricks/secrets/",
    "nexus/bricks/share_link/",
    "nexus/bricks/snapshot/",
    "nexus/bricks/task_manager/",
    "nexus/bricks/upload/",
    "nexus/bricks/versioning/",
    "nexus/bricks/workflows/",
    "nexus/bricks/workspace/",
    # Full search subtree outside of primitives
    "nexus/bricks/search/daemon.py",
    "nexus/bricks/search/search_service.py",
    "nexus/bricks/search/indexing_service.py",
]


@pytest.fixture(scope="module")
def wheel_namelist(slim_wheel: Path) -> list[str]:
    with zipfile.ZipFile(slim_wheel) as zf:
        return zf.namelist()


@pytest.mark.parametrize("required_path", REQUIRED_BRICKS_PATHS)
def test_slim_wheel_includes_required_bricks(wheel_namelist: list[str], required_path: str) -> None:
    # For directory entries, check that at least one file in the wheel
    # starts with the required path prefix (hatchling may not add dir entries).
    in_wheel = any(
        p == required_path or p.startswith(required_path.rstrip("/") + "/") for p in wheel_namelist
    )
    assert in_wheel, f"slim wheel missing: {required_path}\nBricks entries in wheel:\n" + "\n".join(
        p for p in wheel_namelist if "bricks" in p
    )


@pytest.mark.parametrize("forbidden_prefix", FORBIDDEN_BRICKS_PREFIXES)
def test_slim_wheel_excludes_forbidden_bricks(
    wheel_namelist: list[str], forbidden_prefix: str
) -> None:
    leaks = [p for p in wheel_namelist if p.startswith(forbidden_prefix)]
    assert not leaks, (
        f"slim wheel leaked forbidden bricks paths under {forbidden_prefix!r}:\n" + "\n".join(leaks)
    )


def test_slim_wheel_nexus_runtime_dep(slim_wheel: Path) -> None:
    """The METADATA must declare nexus-runtime as Requires-Dist."""
    with zipfile.ZipFile(slim_wheel) as zf:
        meta_files = [n for n in zf.namelist() if n.endswith("METADATA")]
        assert meta_files, f"no METADATA in {slim_wheel}"
        meta = zf.read(meta_files[0]).decode("utf-8")
    assert "Requires-Dist: nexus-runtime" in meta, (
        "slim wheel METADATA missing nexus-runtime requirement.\n"
        "Found Requires-Dist lines:\n"
        + "\n".join(line for line in meta.splitlines() if "Requires-Dist" in line)
    )
