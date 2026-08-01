#!/usr/bin/env python3
"""E2E for #4557: search scope gated on read-capable zone_perms.

Mints real database API keys (write-only / mixed / read / root-scoped)
directly in postgres — hash computed locally with the same default HMAC
salt the server uses when NEXUS_API_KEY_SECRET is unset — then exercises
/api/v2/search/query and /query/batch through the live server.

Adapted from an earlier draft (branch fix/4557-read-gated-search-scope)
to upstream #4558's semantics, which landed independently on develop:
``readable_zone_filter`` / ``token_zone_filter_from_auth`` in
nexus.bricks.search.federated_search, with the router (search.py) failing
CLOSED on both the single-zone and federated paths when a token has no
read-capable zone -- so a write-only token now gets 403 on EITHER path
(the earlier draft's federated route degraded to a 200-empty response
instead; upstream is stricter).

Prerequisites: nexus stack running with this branch's fixes.

Usage:
  PYTHONPATH=src python scripts/test_read_gate_e2e.py
"""

import json
import os
import secrets
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nexus.storage.api_key_ops import hash_api_key  # noqa: E402

API = os.getenv("NEXUS_URL", "http://localhost:2050")
PG_PORT = os.getenv("PG_PORT", "5450")

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} {detail}")
        failed += 1


def curl(method, path, key, params="", data=None):
    url = f"{API}{path}"
    if params:
        url += f"?{params}"
    args = [
        "curl",
        "-s",
        "-w",
        "\n%{http_code}",
        "-X",
        method,
        url,
        "-H",
        f"Authorization: Bearer {key}",
    ]
    if data is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    body, _, status = r.stdout.rpartition("\n")
    parsed = json.loads(body) if body.strip() else {}
    return int(status), parsed


def psql(sql):
    r = subprocess.run(
        [
            "psql",
            "-h",
            "localhost",
            "-p",
            PG_PORT,
            "-U",
            "postgres",
            "-d",
            "nexus",
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PGPASSWORD": "nexus"},
    )
    if r.returncode != 0:
        print(f"  psql error: {r.stderr.strip()}")
    return r.stdout.strip()


def mint_key(name, zones):
    """Insert an api_keys row + api_key_zones junction rows; return raw key.

    zones: list of (zone_id, perms) tuples.
    """
    # secrets.token_hex(16) (32 hex chars) keeps the raw key comfortably
    # above DatabaseAPIKeyAuth's API_KEY_MIN_LENGTH=32 regardless of how
    # short `name` is (a short suffix, e.g. token_hex(8) with "wo"/"ro"
    # names, can land under the minimum and get rejected pre-auth as a
    # malformed key — a 401 unrelated to the #4557 read gate under test).
    raw_key = f"sk-e2e-4557-{name}-{secrets.token_hex(16)}"
    key_hash = hash_api_key(raw_key)
    key_id = secrets.token_hex(16)
    psql(
        "INSERT INTO api_keys (key_id, key_hash, user_id, subject_type, "
        "subject_id, name, is_admin, inherit_permissions, revoked, created_at) "
        f"VALUES ('{key_id}', '{key_hash}', 'e2e_4557_{name}', 'user', "
        f"'e2e_4557_{name}', 'e2e-4557-{name}', 0, 0, 0, NOW()) "
        "ON CONFLICT (key_id) DO NOTHING"
    )
    for zid, perms in zones:
        psql(
            "INSERT INTO api_key_zones (key_id, zone_id, permissions, granted_at) "
            f"VALUES ('{key_id}', '{zid}', '{perms}', NOW()) "
            "ON CONFLICT (key_id, zone_id) DO NOTHING"
        )
    return raw_key


def cleanup_previous_run():
    """Delete rows left behind by a prior run of this script.

    Repeated runs mint fresh api_keys/api_key_zones/rebac_tuples rows every
    time (random key_id/tuple_id suffixes don't collide), so without this
    they accumulate indefinitely in shared dev/CI postgres instances.
    """
    psql(
        "DELETE FROM api_key_zones WHERE key_id IN "
        "(SELECT key_id FROM api_keys WHERE user_id LIKE 'e2e_4557_%')"
    )
    psql("DELETE FROM api_keys WHERE user_id LIKE 'e2e_4557_%'")
    psql("DELETE FROM rebac_tuples WHERE tuple_id LIKE 'zt4557_%'")


def main():
    print("=" * 60)
    print("READ-GATED SEARCH SCOPE E2E (#4557)")
    print("=" * 60)

    # -1. Clean up debris from any prior run before minting fresh rows.
    cleanup_previous_run()

    # 0. Zones (Active phase required by auth-time zone validation)
    for z in ("e2e4557a", "e2e4557b"):
        psql(
            "INSERT INTO zones (zone_id, name, phase, finalizers, created_at, "
            f"updated_at, indexing_mode) VALUES ('{z}', '{z}', 'Active', '[]', "
            "NOW(), NOW(), 'all') ON CONFLICT (zone_id) DO NOTHING"
        )

    # ReBAC: make both zones subject-accessible so federation scope is
    # decided by the TOKEN filter, not by ReBAC discovery.
    for name in ("wo", "mixed", "ro"):
        for z in ("e2e4557a", "e2e4557b"):
            psql(
                "INSERT INTO rebac_tuples (tuple_id, zone_id, subject_zone_id, "
                "object_zone_id, subject_type, subject_id, relation, "
                "object_type, object_id, created_at) VALUES "
                f"('zt4557_{name}_{z}', 'root', 'root', 'root', 'user', "
                f"'e2e_4557_{name}', 'member', 'zone', '{z}', NOW()) "
                "ON CONFLICT (tuple_id) DO NOTHING"
            )

    wo_key = mint_key("wo", [("e2e4557a", "w")])
    mixed_key = mint_key("mixed", [("e2e4557a", "w"), ("e2e4557b", "r")])
    ro_key = mint_key("ro", [("e2e4557a", "r")])
    # #4557 gap 3: an EXPLICIT non-read root grant must not be unbounded.
    # No zone/rebac setup needed for this one -- the gate 403s before any
    # zone access or ReBAC discovery is consulted.
    root_wo_key = mint_key("root_wo", [("root", "w")])

    # 1. Write-only token, single-zone search -> 403. Upstream (#4558)
    # derives token_zone_filter from readable_zone_filter(); a token whose
    # only zone grant is write-only yields the EMPTY frozenset, which the
    # router's fail-closed check 403s unconditionally (before the
    # federated/single-zone branch split even matters).
    status, body = curl("GET", "/api/v2/search/query", wo_key, "q=alpha")
    print(f"  wo single-zone: {status} {body.get('detail', '')}")
    check("write-only single-zone search rejected 403", status == 403)
    check(
        "403 detail names the read-permission gate, not the full allow-list",
        "read permission" in str(body.get("detail", ""))
        and "e2e4557b" not in str(body.get("detail", "")),
    )

    # 2. Write-only token, federated=true -> 403 (CHANGED from the earlier
    # draft's 200-empty: upstream's empty-filter check runs regardless of
    # the federated flag, so a write-only token fails closed on BOTH paths).
    status, body = curl("GET", "/api/v2/search/query", wo_key, "q=alpha&federated=true")
    print(f"  wo federated: {status} {body.get('detail', '')}")
    check("write-only federated search also rejected 403 (fails closed)", status == 403)
    check(
        "403 detail names the read-permission gate",
        "read permission" in str(body.get("detail", "")),
    )

    # 3. Mixed token (a:w, b:r) auto-federates; scope excludes the w-only zone
    status, body = curl("GET", "/api/v2/search/query", mixed_key, "q=alpha")
    print(
        f"  mixed: {status} zones_searched={body.get('zones_searched')} total={body.get('total')}"
    )
    check("mixed token search returns 200", status == 200)
    check(
        "mixed token never searches the write-only zone",
        "e2e4557a" not in (body.get("zones_searched") or []),
    )

    # 4. Read token, single-zone search -> 200 (+ hot-path latency smoke:
    # the gate is O(len(zone_perms)) pure python — anything visible in
    # latency_ms would indicate an accidental extra round-trip)
    status, body = curl("GET", "/api/v2/search/query", ro_key, "q=alpha")
    print(
        f"  ro single-zone: {status} total={body.get('total')} latency_ms={body.get('latency_ms')}"
    )
    check("read token single-zone search allowed", status == 200)
    check(
        "read-gated query latency sane (<2000ms)",
        isinstance(body.get("latency_ms"), (int, float)) and body["latency_ms"] < 2000,
    )

    # 5. Batch endpoint mirrors the gate (#4557 gap 1: /query/batch had no
    # read gate at all prior to this branch).
    status, body = curl(
        "POST",
        "/api/v2/search/query/batch",
        wo_key,
        data={"queries": [{"q": "alpha"}]},
    )
    print(f"  wo batch: {status} {body.get('detail', '')}")
    check("write-only batch rejected 403", status == 403)
    check(
        "batch 403 detail names the read-permission gate",
        "read permission" in str(body.get("detail", "")),
    )
    status, body = curl(
        "POST",
        "/api/v2/search/query/batch",
        ro_key,
        data={"queries": [{"q": "alpha"}]},
    )
    check("read token batch allowed", status == 200)

    # 6. #4557 gap 3: explicit root:"w" grant must not be unbounded. Before
    # the fix, ANY token whose zone_set was exactly {root} returned
    # zone_filter=None (unbounded) without ever consulting the granted
    # letters -- a root:"w" credential could search everything.
    status, body = curl("GET", "/api/v2/search/query", root_wo_key, "q=alpha")
    print(f"  root write-only: {status} {body.get('detail', '')}")
    check("explicit root:w grant rejected 403 (not unbounded)", status == 403)
    check(
        "root 403 detail names the read-permission gate",
        "read permission" in str(body.get("detail", "")),
    )

    print("=" * 60)
    print(f"PASSED: {passed}  FAILED: {failed}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
