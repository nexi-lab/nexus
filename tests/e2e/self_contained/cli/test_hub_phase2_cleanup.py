"""E2E: Phase 2 cleanup of #3871 — multi-zone token through nexus up --build.

Drives `nexus hub token create --zones eng:rw,ops:rw` against a real stack
and asserts the deprecated APIKeyModel.zone_id column stays NULL while the
junction is the source of truth. Mirrors the skip pattern from
test_hub_flow.py: skips cleanly if NEXUS_ADMIN_URL / NEXUS_ADMIN_KEY /
NEXUS_DATABASE_URL / MCP_HTTP_URL is unset.

Live-stack execution is deferred to CI; running without those env vars
produces 4 SKIPPED, no errors.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.e2e]


def _nexus_bin() -> str:
    """Resolve the ``nexus`` console script in the active venv."""
    return str(Path(sys.executable).parent / "nexus")


def _required_env() -> dict[str, str]:
    """Return required env vars or skip the test if any are missing."""
    keys = ("NEXUS_ADMIN_URL", "NEXUS_ADMIN_KEY", "NEXUS_DATABASE_URL", "MCP_HTTP_URL")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(
            f"missing env vars: {missing} — requires a running nexus stack; "
            "see `nexus-stack` skill or CI"
        )
    return {k: os.environ[k] for k in keys}


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``nexus <args>`` with the given env, capturing text output."""
    return subprocess.run(
        [_nexus_bin(), *args],
        env={**os.environ, **env},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _extract_field(stdout: str, field: str) -> str:
    """Parse a ``field: value`` line from hub token create output."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            return stripped.split(":", 1)[1].strip()
    raise AssertionError(f"no '{field}:' line in create output:\n{stdout}")


def _engine(env: dict[str, str]):
    """Build a SQLAlchemy engine from NEXUS_DATABASE_URL."""
    return create_engine(env["NEXUS_DATABASE_URL"])


def test_phase2_token_create_persists_null_zone_id_and_populates_junction() -> None:
    """Phase 2: api_keys.zone_id must be NULL; junction must carry both zones."""
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"

    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name],
        env,
    )
    assert create.returncode == 0, (
        f"hub token create failed\nstdout: {create.stdout}\nstderr: {create.stderr}"
    )
    key_id = _extract_field(create.stdout, "key_id")

    try:
        eng = _engine(env)
        with eng.connect() as conn:
            zone_id_value = conn.execute(
                text("SELECT zone_id FROM api_keys WHERE key_id = :k"), {"k": key_id}
            ).scalar_one()
            junction_zones = {
                r[0]
                for r in conn.execute(
                    text("SELECT zone_id FROM api_key_zones WHERE key_id = :k"), {"k": key_id}
                )
            }

        assert zone_id_value is None, (
            f"Phase 2: api_keys.zone_id must be NULL, got {zone_id_value!r}"
        )
        assert junction_zones == {"eng", "ops"}, (
            f"Expected junction zones {{'eng', 'ops'}}, got {junction_zones}"
        )
    finally:
        _run(["hub", "token", "revoke", name], env)


def test_phase2_token_list_emits_primary_in_deprecated_zone_field() -> None:
    """token list --json must populate 'zone' (deprecated alias) from get_primary_zone."""
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"

    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name],
        env,
    )
    assert create.returncode == 0, (
        f"hub token create failed\nstdout: {create.stdout}\nstderr: {create.stderr}"
    )
    bob_id = _extract_field(create.stdout, "key_id")

    try:
        listed = _run(["hub", "token", "list", "--json"], env)
        assert listed.returncode == 0, listed.stderr

        # Output is {"tokens": [...]} — not a top-level array.
        tokens = json.loads(listed.stdout)["tokens"]
        bob_row = next((r for r in tokens if r["key_id"] == bob_id), None)
        assert bob_row is not None, f"{bob_id!r} not found in listed tokens"
        assert bob_row["zone"] == "eng", (
            f"deprecated 'zone' alias must equal primary (MIN granted_at); got {bob_row['zone']!r}"
        )
        assert set(bob_row["zones"]) == {"eng", "ops"}, (
            f"'zones' field must contain both zones; got {bob_row['zones']!r}"
        )
    finally:
        _run(["hub", "token", "revoke", name], env)


def test_phase2_mcp_request_accepts_multi_zone_bearer() -> None:
    """Bearer token for a multi-zone key must not be rejected (not 401) by the MCP endpoint."""
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"

    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name],
        env,
    )
    assert create.returncode == 0, (
        f"hub token create failed\nstdout: {create.stdout}\nstderr: {create.stderr}"
    )
    raw_token = _extract_field(create.stdout, "token")

    try:
        # POST an MCP initialize request — we only care that the auth pipeline
        # accepts the token (not 401). Even an invalid protocol body returns
        # a non-401 status when auth succeeds.
        resp = httpx.post(
            f"{env['MCP_HTTP_URL']}/mcp",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "phase2-e2e", "version": "1"},
                },
            },
            timeout=10.0,
        )
        assert resp.status_code != 401, (
            f"valid multi-zone bearer token rejected (status {resp.status_code}): {resp.text[:300]}"
        )
    finally:
        _run(["hub", "token", "revoke", name], env)


def test_phase2_admin_list_filtered_by_zone_returns_multi_zone_key() -> None:
    """Admin REST list filter must use junction — multi-zone key appears under non-primary zone."""
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"

    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name],
        env,
    )
    assert create.returncode == 0, (
        f"hub token create failed\nstdout: {create.stdout}\nstderr: {create.stderr}"
    )
    dave_id = _extract_field(create.stdout, "key_id")

    try:
        # Filter by ops via REST. Pre-Phase-2, the WHERE zone_id='ops' filter would
        # have missed `dave` (his primary by granted_at is 'eng'). Post-Phase-2,
        # the junction join matches every granted zone.
        headers = {"Authorization": f"Bearer {env['NEXUS_ADMIN_KEY']}"}
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{env['NEXUS_ADMIN_URL']}/api/v2/auth/keys",
                headers=headers,
                params={"zone_id": "ops"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # handle_admin_list_keys returns {"keys": [...], "total": N}
        keys = body["keys"] if isinstance(body, dict) else body
        ids = [k["key_id"] for k in keys]
        assert dave_id in ids, (
            f"Multi-zone key {dave_id!r} not found in ops-filtered list. "
            f"Phase 2 junction filter may not be active. IDs returned: {ids}"
        )
    finally:
        _run(["hub", "token", "revoke", name], env)
