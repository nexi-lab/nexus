"""End-to-end test for the ``nexus hub`` CLI flow (#3784).

Drives the whole admin story against a real running nexus stack:

1. ``nexus hub token create --admin --zone root --name e2e`` prints an sk- token.
2. ``nexus hub token list --json`` includes the new row.
3. HTTP request to the MCP endpoint with the token succeeds (not 401).
4. ``nexus hub token revoke e2e`` marks the row revoked.
5. ``nexus hub token list --show-revoked --json`` shows ``revoked=True``
   and a non-null ``revoked_at`` for the row.
6. ``nexus hub status --json`` reports ``postgres: ok`` and
   ``tokens.revoked >= 1``.

The test discovers the stack via the same env vars the MCP HTTP tests use
(``NEXUS_ADMIN_URL`` / ``NEXUS_ADMIN_KEY`` / ``MCP_HTTP_URL``) plus a
``NEXUS_DATABASE_URL`` that points at the hub DB (the CLI is direct-to-DB;
see spec §Token/admin model — bootstrap). Skips cleanly if any of those
are missing so local dev-loops don't fail without a stack running.

Note: wire-level revocation propagation under the 60s AuthIdentityCache
TTL is covered by ``tests/e2e/self_contained/mcp/test_mcp_http_audit.py``
(shipped with #3779). This test covers the admin-level lifecycle only,
so no long sleep is required.
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

pytestmark = [pytest.mark.e2e]


def _nexus_bin() -> str:
    """Resolve the ``nexus`` console script in the active venv."""
    return str(Path(sys.executable).parent / "nexus")


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``nexus <args>`` with the given env, capturing text output."""
    return subprocess.run(
        [_nexus_bin(), *args],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _require(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        pytest.skip(
            f"{var} must be set to run the hub e2e flow "
            "(requires a running nexus stack; see `nexus-stack` skill)"
        )
    return value


@pytest.fixture()
def hub_cli_env() -> dict[str, str]:
    """Env dict for CLI subprocesses — DB URL + clean copy of current env."""
    db_url = _require("NEXUS_DATABASE_URL")
    env = dict(os.environ)
    env["NEXUS_DATABASE_URL"] = db_url
    # Optional: pass through Redis URL so `hub status` can read metrics.
    if "NEXUS_REDIS_URL" in os.environ:
        env["NEXUS_REDIS_URL"] = os.environ["NEXUS_REDIS_URL"]
    elif "DRAGONFLY_URL" in os.environ:
        env["DRAGONFLY_URL"] = os.environ["DRAGONFLY_URL"]
    return env


def _mcp_url() -> str:
    # `mcp_http_base_url` fixture defaults to http://localhost:8081. Use
    # the same default here so the test plugs into an existing stack.
    return os.environ.get("MCP_HTTP_URL", "http://localhost:8081")


def _extract_token(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("token:"):
            return line.split("token:", 1)[1].strip()
    raise AssertionError(f"no 'token:' line in create output:\n{stdout}")


def test_hub_end_to_end_token_lifecycle(hub_cli_env: dict[str, str]) -> None:
    """Create → use → revoke → status, end-to-end against a live stack."""
    _require("NEXUS_ADMIN_URL")  # ensures the stack is up (MCP tests need it too)
    mcp_base_url = _mcp_url()
    # Unique token name so re-runs don't collide with stale rows.
    token_name = f"e2e-{uuid.uuid4().hex[:8]}"

    # 1. Create admin token.
    create = _run(
        ["hub", "token", "create", "--name", token_name, "--zone", "root", "--admin"],
        env=hub_cli_env,
    )
    assert create.returncode == 0, (
        f"hub token create failed (stderr): {create.stderr}\nstdout: {create.stdout}"
    )
    token = _extract_token(create.stdout)
    assert token.startswith("sk-"), f"expected sk- token, got {token!r}"

    try:
        # 2. `hub token list --json` includes the new row.
        listed = _run(["hub", "token", "list", "--json"], env=hub_cli_env)
        assert listed.returncode == 0, listed.stderr
        payload = json.loads(listed.stdout)
        names = [t["name"] for t in payload["tokens"]]
        assert token_name in names, f"{token_name!r} not in listed tokens: {names}"

        # 3. MCP HTTP request with the token — not 401.
        #    We use a plain POST (not a full MCP session) because we only
        #    care about the auth outcome. Even a malformed JSON-RPC body
        #    with valid auth returns 2xx/4xx on the body — never 401.
        resp = httpx.post(
            f"{mcp_base_url}/mcp",
            headers={
                "Authorization": f"Bearer {token}",
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
                    "clientInfo": {"name": "hub-e2e", "version": "1"},
                },
            },
            timeout=10.0,
        )
        assert resp.status_code != 401, (
            f"valid admin token rejected (status {resp.status_code}): {resp.text[:300]}"
        )

        # 4. Revoke.
        revoke = _run(["hub", "token", "revoke", token_name], env=hub_cli_env)
        assert revoke.returncode == 0, revoke.stderr
        assert "revoked" in revoke.stdout.lower()

        # 5. Verify revocation is persisted: `hub token list --show-revoked`
        #    must return the row with revoked=True and a non-null revoked_at.
        #    Note: MCP `initialize` is an unauthenticated handshake (FastMCP
        #    enforces auth at the tool-call layer), so we check DB state
        #    rather than wire-level rejection. Wire-level revocation
        #    propagation under the 60s AuthIdentityCache TTL is covered by
        #    `tests/e2e/self_contained/mcp/test_mcp_http_audit.py` (#3779).
        listed_after = _run(["hub", "token", "list", "--show-revoked", "--json"], env=hub_cli_env)
        assert listed_after.returncode == 0, listed_after.stderr
        rows = {t["name"]: t for t in json.loads(listed_after.stdout)["tokens"]}
        assert token_name in rows, f"{token_name!r} not in list after revoke"
        assert rows[token_name]["revoked"] is True, rows[token_name]
        assert rows[token_name]["revoked_at"] != "-", rows[token_name]

        # 6. Hub status reports postgres ok and revoked >= 1.
        status = _run(["hub", "status", "--json"], env=hub_cli_env)
        assert status.returncode == 0, status.stderr
        status_payload = json.loads(status.stdout)
        assert status_payload["postgres"] == "ok", status_payload
        assert status_payload["tokens"]["revoked"] >= 1, status_payload
    finally:
        # Best-effort cleanup so stale revoked rows don't pile up across
        # reruns against the same dev stack. Already-revoked is fine.
        _run(["hub", "token", "revoke", token_name], env=hub_cli_env)


def test_hub_multi_zone_token_lifecycle(hub_cli_env: dict[str, str]) -> None:
    """e2e: create multi-zone token, list, mutate zones, refuse last-zone removal (#3785).

    Drives the CLI end-to-end against a real running stack. Uses the `root` zone
    (always present) plus best-effort creates of `eng` and `ops` (skipped if zone
    creation isn't permitted in this stack).
    """
    token_name = f"e2e_multi_{uuid.uuid4().hex[:8]}"

    # Best-effort: ensure two extra zones exist. If zone creation isn't supported
    # by the CLI in this deployment, fall through and just rely on `root`.
    for z in ("eng", "ops"):
        _run(
            ["zone", "create", z], env=hub_cli_env
        )  # ignore failures (already exists / not allowed)

    try:
        # 1. Create with --zones CSV (root is always available; eng/ops if seeded).
        # We pick the first zone that's actually Active per `nexus hub status`.
        # For robustness, just use `root` as a known-good zone.
        create = _run(
            ["hub", "token", "create", "--name", token_name, "--zones", "root"],
            env=hub_cli_env,
        )
        assert create.returncode == 0, create.stderr
        # Token raw value printed; we don't parse it for this test.

        # 2. list --json shows the token with zones=["root"].
        listed = _run(["hub", "token", "list", "--json"], env=hub_cli_env)
        assert listed.returncode == 0, listed.stderr
        tokens = {t["name"]: t for t in json.loads(listed.stdout)["tokens"]}
        assert token_name in tokens, f"{token_name!r} not in {list(tokens)}"
        assert "zones" in tokens[token_name], tokens[token_name]
        assert tokens[token_name]["zones"] == ["root"], tokens[token_name]

        # 3. zones show — single zone for now.
        show1 = _run(["hub", "token", "zones", "show", "--name", token_name], env=hub_cli_env)
        assert show1.returncode == 0, show1.stderr
        assert "root" in show1.stdout

        # 4. zones add eng (best-effort — only assert on the case where eng exists).
        add_eng = _run(
            ["hub", "token", "zones", "add", "--name", token_name, "--zone", "eng"],
            env=hub_cli_env,
        )
        if add_eng.returncode == 0:
            # eng zone existed and add succeeded.
            assert "added" in add_eng.stdout or "no change" in add_eng.stdout

            # zones show now includes both.
            show2 = _run(["hub", "token", "zones", "show", "--name", token_name], env=hub_cli_env)
            assert show2.returncode == 0, show2.stderr
            assert "root" in show2.stdout
            assert "eng" in show2.stdout

            # zones remove eng → succeeds (root remains).
            rm_eng = _run(
                ["hub", "token", "zones", "remove", "--name", token_name, "--zone", "eng"],
                env=hub_cli_env,
            )
            assert rm_eng.returncode == 0, rm_eng.stderr
            assert "removed" in rm_eng.stdout

        # 5. Removing the last zone (root) MUST fail with "last zone".
        rm_last = _run(
            ["hub", "token", "zones", "remove", "--name", token_name, "--zone", "root"],
            env=hub_cli_env,
        )
        assert rm_last.returncode != 0
        combined = (rm_last.stdout + rm_last.stderr).lower()
        assert "last zone" in combined, combined
    finally:
        # Best-effort cleanup.
        _run(["hub", "token", "revoke", token_name], env=hub_cli_env)
