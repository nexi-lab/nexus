# Hub Status Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `nexus hub status --detail` with richer local hub diagnostics while preserving default `nexus hub status` output.

**Architecture:** Keep the hub status command local-first. `hub.py` owns payload assembly and text/JSON rendering; existing MCP audit and rate-limit middleware write small best-effort Redis counters that the CLI reads. Detail metrics degrade to `null`/`n/a`, while Postgres remains the only hard health dependency.

**Tech Stack:** Python, Click, SQLAlchemy, Redis/Dragonfly, Starlette middleware, pytest.

---

## File Structure

- Modify `src/nexus/cli/commands/hub.py`: add `--detail`, split status payload/render helpers, collect Postgres token/zone details, read detail Redis counters, and compute local Zoekt index metadata.
- Modify `src/nexus/bricks/mcp/middleware_audit.py`: add per-zone QPS and active-client counters.
- Modify `src/nexus/bricks/mcp/middleware_ratelimit.py`: add best-effort per-tier 429 counters.
- Modify `tests/unit/cli/test_hub.py`: cover default output preservation, detail JSON/text shape, token last-seen, Redis detail aggregation, and search metadata.
- Modify `tests/unit/bricks/mcp/test_middleware_audit_metrics.py`: cover per-zone counter writes.
- Modify `tests/unit/bricks/mcp/test_middleware_ratelimit.py`: cover 429 counter recording.

## Task 1: Preserve Base Status Contract And Add `--detail` Flag

**Files:**
- Modify: `tests/unit/cli/test_hub.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Add a failing contract test for default text output**

Add this test near the existing hub status tests in `tests/unit/cli/test_hub.py`:

```python
def test_hub_status_text_unchanged_without_detail(monkeypatch):
    monkeypatch.setenv("NEXUS_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("NEXUS_MCP_PORT", "8081")
    monkeypatch.setenv("NEXUS_PROFILE", "full")

    session = MagicMock()
    session.execute.return_value.scalar.side_effect = [5, 2]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": 3.5, "connections": 4, "redis": "ok"},
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["status"])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "endpoint:    http://0.0.0.0:8081/mcp\n"
        "profile:     full\n"
        "postgres:    ok\n"
        "redis:       ok\n"
        "tokens:      5 active, 2 revoked\n"
        "connections: 4\n"
        "qps (5m):    3.5\n"
    )
```

- [ ] **Step 2: Run the contract test and verify it passes before refactor**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_hub_status_text_unchanged_without_detail -v
```

Expected: `PASSED`. If it fails, stop and inspect the current output before refactoring.

- [ ] **Step 3: Add a failing test that the new flag is accepted**

Add this test after `test_hub_status_text_unchanged_without_detail`:

```python
def test_hub_status_detail_flag_is_accepted(monkeypatch):
    session = MagicMock()
    session.execute.return_value.scalar.side_effect = [0, 0]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": None, "connections": None, "redis": "n/a"},
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._collect_status_detail",
        lambda _zone_ids, _tokens_detail, _redis_detail: {
            "zones": [],
            "tokens_detail": [],
            "rate_limits": {"window_seconds": 300, "hits_by_tier": {}},
            "search": {"zones": []},
        },
        raising=False,
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_detail_stats",
        lambda _zone_ids: {
            "zones": [],
            "rate_limits": {"window_seconds": 300, "hits_by_tier": {}},
        },
        raising=False,
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["status", "--detail", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["detail"] is True
```

- [ ] **Step 4: Run the flag test and verify it fails**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_hub_status_detail_flag_is_accepted -v
```

Expected: `FAILED` with Click reporting `No such option: --detail`.

- [ ] **Step 5: Refactor `hub_status` minimally and add the flag**

In `src/nexus/cli/commands/hub.py`, replace the current `hub_status` function with helpers plus a `detail` option. Keep `_read_redis_stats()` unchanged.

```python
def _display_status_value(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _collect_postgres_status() -> dict[str, Any]:
    pg_state = "ok"
    active = revoked = 0
    try:
        factory = get_session_factory()
        with factory() as session:
            active = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 0)
                ).scalar()
                or 0
            )
            revoked = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 1)
                ).scalar()
                or 0
            )
    except Exception:  # noqa: BLE001
        pg_state = "err"
    return {
        "postgres": pg_state,
        "tokens": {"active": int(active), "revoked": int(revoked)},
        "zone_ids": [],
        "tokens_detail": [],
    }


def _base_status_payload() -> dict[str, Any]:
    host = os.environ.get("NEXUS_MCP_HOST", "0.0.0.0")
    port = os.environ.get("NEXUS_MCP_PORT", "8081")
    profile = os.environ.get("NEXUS_PROFILE", "full")
    endpoint = f"http://{host}:{port}/mcp"

    postgres_status = _collect_postgres_status()
    redis_stats = _read_redis_stats()
    payload = {
        "endpoint": endpoint,
        "profile": profile,
        "postgres": postgres_status["postgres"],
        "redis": redis_stats["redis"],
        "tokens": postgres_status["tokens"],
        "connections": redis_stats["connections"],
        "qps_5m": redis_stats["qps_5m"],
    }
    return payload


def _emit_base_status_text(payload: dict[str, Any]) -> None:
    click.echo(f"endpoint:    {payload['endpoint']}")
    click.echo(f"profile:     {payload['profile']}")
    click.echo(f"postgres:    {payload['postgres']}")
    click.echo(f"redis:       {payload['redis']}")
    click.echo(
        f"tokens:      {payload['tokens']['active']} active, "
        f"{payload['tokens']['revoked']} revoked"
    )
    click.echo("connections: " f"{_display_status_value(payload['connections'])}")
    click.echo(f"qps (5m):    {_display_status_value(payload['qps_5m'])}")


def _read_redis_detail_stats(zone_ids: list[str]) -> dict[str, Any]:
    return {
        "zones": [{"zone_id": zone_id, "clients": None, "qps_5m": None} for zone_id in zone_ids],
        "rate_limits": {"window_seconds": 300, "hits_by_tier": {}},
    }


def _collect_status_detail(
    zone_ids: list[str],
    tokens_detail: list[dict[str, Any]],
    redis_detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "zones": redis_detail["zones"],
        "tokens_detail": tokens_detail,
        "rate_limits": redis_detail["rate_limits"],
        "search": _collect_search_detail(zone_ids),
    }


def _collect_search_detail(zone_ids: list[str]) -> dict[str, Any]:
    return {
        "zones": [
            {
                "zone_id": zone_id,
                "zoekt_index_size_bytes": None,
                "zoekt_index_size_display": None,
                "zoekt_last_indexed": None,
                "txtai_queue_depth": None,
                "last_indexed": None,
            }
            for zone_id in zone_ids
        ]
    }


def _emit_detail_status_text(payload: dict[str, Any]) -> None:
    click.echo("")
    click.echo("zones:")
    click.echo(
        format_table(
            headers=["zone", "clients", "qps_5m"],
            rows=[
                [
                    row["zone_id"],
                    _display_status_value(row.get("clients")),
                    _display_status_value(row.get("qps_5m")),
                ]
                for row in payload.get("zones", [])
            ],
        )
    )
    click.echo("")
    click.echo("tokens:")
    click.echo(
        format_table(
            headers=["key_id", "name", "zones", "admin", "last_seen"],
            rows=[
                [
                    row["key_id"],
                    row["name"],
                    ",".join(row.get("zones", [])),
                    "yes" if row.get("admin") else "no",
                    _display_status_value(row.get("last_seen")),
                ]
                for row in payload.get("tokens_detail", [])
            ],
        )
    )
    click.echo("")
    click.echo("rate limits:")
    hits = payload.get("rate_limits", {}).get("hits_by_tier", {})
    click.echo(
        format_table(
            headers=["tier", "hits_5m"],
            rows=[[tier, _display_status_value(hits[tier])] for tier in sorted(hits)],
        )
    )
    click.echo("")
    click.echo("search:")
    click.echo(
        format_table(
            headers=["zone", "zoekt_size", "zoekt_last_indexed", "txtai_queue_depth", "last_indexed"],
            rows=[
                [
                    row["zone_id"],
                    _display_status_value(row.get("zoekt_index_size_display")),
                    _display_status_value(row.get("zoekt_last_indexed")),
                    _display_status_value(row.get("txtai_queue_depth")),
                    _display_status_value(row.get("last_indexed")),
                ]
                for row in payload.get("search", {}).get("zones", [])
            ],
        )
    )


@hub.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--detail", is_flag=True, help="Include per-zone, per-token, rate-limit, and search detail.")
def hub_status(as_json: bool, detail: bool) -> None:
    """Show hub health: postgres, redis, tokens, connections, qps."""
    import json as _json

    payload = _base_status_payload()
    if detail:
        redis_detail = _read_redis_detail_stats([])
        payload["detail"] = True
        payload.update(_collect_status_detail([], [], redis_detail))

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
    else:
        _emit_base_status_text(payload)
        if detail:
            _emit_detail_status_text(payload)

    if payload["postgres"] != "ok":
        raise SystemExit(2)
```

- [ ] **Step 6: Run status tests and verify the contract still passes**

Run:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
```

Expected: all selected tests pass, including the new default-output contract test.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3874): add hub status detail flag scaffold"
```

## Task 2: Add Postgres Detail Rows For Zones And Tokens

**Files:**
- Modify: `tests/unit/cli/test_hub.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Add a failing SQLite-backed detail JSON test**

Add this test near the status tests:

```python
def test_hub_status_detail_json_includes_token_last_seen_and_zones(monkeypatch):
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import APIKeyModel, APIKeyZoneModel, Base, ZoneModel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    created = datetime(2026, 5, 5, 13, 0)
    last_seen = datetime(2026, 5, 5, 14, 20, 10)

    with Session() as s, s.begin():
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.add(
            APIKeyModel(
                key_id="kid_alice",
                key_hash="hash_alice",
                user_id="alice",
                name="alice",
                is_admin=0,
                created_at=created,
                last_used_at=last_seen,
                revoked=0,
            )
        )
        s.add(APIKeyZoneModel(key_id="kid_alice", zone_id="eng", permissions="rw"))
        s.add(APIKeyZoneModel(key_id="kid_alice", zone_id="ops", permissions="r"))

    monkeypatch.setattr("nexus.cli.commands.hub.get_session_factory", lambda: Session)
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": 0.0, "connections": 0, "redis": "ok"},
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_detail_stats",
        lambda zone_ids: {
            "zones": [{"zone_id": zone_id, "clients": None, "qps_5m": None} for zone_id in zone_ids],
            "rate_limits": {"window_seconds": 300, "hits_by_tier": {}},
        },
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._collect_search_detail",
        lambda zone_ids: {"zones": [{"zone_id": zone_id, "txtai_queue_depth": None} for zone_id in zone_ids]},
        raising=False,
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["status", "--detail", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["zone_id"] for row in payload["zones"]] == ["eng", "ops"]
    assert payload["tokens_detail"] == [
        {
            "key_id": "kid_alice",
            "name": "alice",
            "zones": ["eng", "ops"],
            "admin": False,
            "created": created.isoformat(),
            "last_seen": last_seen.isoformat(),
            "revoked": False,
            "revoked_at": None,
        }
    ]
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_hub_status_detail_json_includes_token_last_seen_and_zones -v
```

Expected: `FAILED` because `tokens_detail` is empty and zone IDs are not passed into Redis/search detail helpers yet.

- [ ] **Step 3: Implement Postgres detail collection**

In `src/nexus/cli/commands/hub.py`, add these helpers near the status helpers:

```python
def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _collect_zone_ids(session: Any) -> list[str]:
    rows = (
        session.execute(
            select(ZoneModel.zone_id)
            .where(ZoneModel.phase == "Active")
            .where(ZoneModel.deleted_at.is_(None))
            .order_by(ZoneModel.zone_id.asc())
        )
        .scalars()
        .all()
    )
    return [str(zone_id) for zone_id in rows]


def _token_zones_by_key(session: Any, key_ids: list[str]) -> dict[str, list[str]]:
    if not key_ids:
        return {}
    rows = (
        session.execute(
            select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id)
            .where(APIKeyZoneModel.key_id.in_(key_ids))
            .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
        )
        .all()
    )
    zones_by_key: dict[str, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, zone_id in rows:
        zones_by_key.setdefault(key_id, []).append(zone_id)
    return zones_by_key


def _collect_token_detail(session: Any) -> list[dict[str, Any]]:
    rows = (
        session.execute(select(APIKeyModel).order_by(APIKeyModel.created_at.desc()))
        .scalars()
        .all()
    )
    key_ids = [row.key_id for row in rows]
    zones_by_key = _token_zones_by_key(session, key_ids)
    return [
        {
            "key_id": row.key_id,
            "name": row.name,
            "zones": zones_by_key.get(row.key_id, []),
            "admin": bool(row.is_admin),
            "created": _iso_or_none(row.created_at),
            "last_seen": _iso_or_none(row.last_used_at),
            "revoked": bool(row.revoked),
            "revoked_at": _iso_or_none(row.revoked_at),
        }
        for row in rows
    ]
```

Then replace `_collect_postgres_status` with a detail-aware version that performs every DB read inside the session context:

```python
def _collect_postgres_status(detail: bool = False) -> dict[str, Any]:
    pg_state = "ok"
    active = revoked = 0
    zone_ids: list[str] = []
    tokens_detail: list[dict[str, Any]] = []
    try:
        factory = get_session_factory()
        with factory() as session:
            active = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 0)
                ).scalar()
                or 0
            )
            revoked = (
                session.execute(
                    select(func.count()).select_from(APIKeyModel).where(APIKeyModel.revoked == 1)
                ).scalar()
                or 0
            )
            if detail:
                zone_ids = _collect_zone_ids(session)
                tokens_detail = _collect_token_detail(session)
    except Exception:  # noqa: BLE001
        pg_state = "err"
    return {
        "postgres": pg_state,
        "tokens": {"active": int(active), "revoked": int(revoked)},
        "zone_ids": zone_ids,
        "tokens_detail": tokens_detail,
    }
```

Then change `_base_status_payload` so it accepts the already-collected Postgres status:

```python
def _base_status_payload(postgres_status: dict[str, Any]) -> dict[str, Any]:
    host = os.environ.get("NEXUS_MCP_HOST", "0.0.0.0")
    port = os.environ.get("NEXUS_MCP_PORT", "8081")
    profile = os.environ.get("NEXUS_PROFILE", "full")
    endpoint = f"http://{host}:{port}/mcp"

    redis_stats = _read_redis_stats()
    return {
        "endpoint": endpoint,
        "profile": profile,
        "postgres": postgres_status["postgres"],
        "redis": redis_stats["redis"],
        "tokens": postgres_status["tokens"],
        "connections": redis_stats["connections"],
        "qps_5m": redis_stats["qps_5m"],
    }
```

Keep `_collect_status_detail` session-free:

```python
def _collect_status_detail(
    zone_ids: list[str],
    tokens_detail: list[dict[str, Any]],
    redis_detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "zones": redis_detail["zones"],
        "tokens_detail": tokens_detail,
        "rate_limits": redis_detail["rate_limits"],
        "search": _collect_search_detail(zone_ids),
    }
```

Update `hub_status` so DB detail is collected before Redis/search detail:

```python
    postgres_status = _collect_postgres_status(detail=detail)
    payload = _base_status_payload(postgres_status)
    if detail:
        zone_ids = postgres_status["zone_ids"]
        redis_detail = _read_redis_detail_stats(zone_ids)
        payload["detail"] = True
        payload.update(
            _collect_status_detail(zone_ids, postgres_status["tokens_detail"], redis_detail)
        )
```

- [ ] **Step 4: Verify the temporary search detail stub still exists**

Task 1 added this helper. Keep it in place so Task 2 is independently green:

```python
def _collect_search_detail(zone_ids: list[str]) -> dict[str, Any]:
    return {
        "zones": [
            {
                "zone_id": zone_id,
                "zoekt_index_size_bytes": None,
                "zoekt_index_size_display": None,
                "zoekt_last_indexed": None,
                "txtai_queue_depth": None,
                "last_indexed": None,
            }
            for zone_id in zone_ids
        ]
    }
```

- [ ] **Step 5: Run the detail Postgres test**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_hub_status_detail_json_includes_token_last_seen_and_zones -v
```

Expected: `PASSED`.

- [ ] **Step 6: Run all status tests**

Run:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3874): include token and zone detail in hub status"
```

## Task 3: Read Per-Zone And Rate-Limit Detail From Redis

**Files:**
- Modify: `tests/unit/cli/test_hub.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Add a failing unit test for Redis detail aggregation**

Add this test near the status tests:

```python
def test_read_redis_detail_stats_aggregates_zone_and_rate_limit_counts(monkeypatch):
    import nexus.cli.commands.hub as hub_module

    class FakeRedis:
        def __init__(self):
            self.mget_calls = []
            self.scard_calls = []

        def ping(self):
            return True

        def mget(self, keys):
            self.mget_calls.append(keys)
            values = {
                "nexus:hub:qps:zone:eng:100": b"120",
                "nexus:hub:qps:zone:eng:99": b"30",
                "nexus:hub:qps:zone:ops:100": b"60",
                "nexus:hub:rate_limit:anonymous:100": b"2",
                "nexus:hub:rate_limit:authenticated:100": b"4",
            }
            return [values.get(key) for key in keys]

        def scard(self, key):
            self.scard_calls.append(key)
            return {"nexus:hub:active:zone:eng:100": 3, "nexus:hub:active:zone:ops:100": 1}.get(key, 0)

    fake = FakeRedis()
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setattr("redis.from_url", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(hub_module.time, "time", lambda: 100 * 60)

    detail = hub_module._read_redis_detail_stats(["eng", "ops"])

    assert detail["zones"] == [
        {"zone_id": "eng", "clients": 3, "qps_5m": 0.5},
        {"zone_id": "ops", "clients": 1, "qps_5m": 0.2},
    ]
    assert detail["rate_limits"] == {
        "window_seconds": 300,
        "hits_by_tier": {"anonymous": 2, "authenticated": 4, "premium": 0},
    }
```

- [ ] **Step 2: Run the Redis detail test and verify it fails**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_read_redis_detail_stats_aggregates_zone_and_rate_limit_counts -v
```

Expected: `FAILED` because `_read_redis_detail_stats` returns stub `None` values.

- [ ] **Step 3: Import module-level `time` in `hub.py`**

At the top of `src/nexus/cli/commands/hub.py`, add:

```python
import time
```

Then remove the inner `import time` from `_read_redis_stats()` so tests can monkeypatch `nexus.cli.commands.hub.time.time`.

- [ ] **Step 4: Replace `_read_redis_detail_stats` with Redis-backed implementation**

Replace the stub helper with:

```python
_RATE_LIMIT_TIERS = ("anonymous", "authenticated", "premium")


def _decode_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_redis_detail_stats(zone_ids: list[str]) -> dict[str, Any]:
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    empty_zones = [{"zone_id": zone_id, "clients": None, "qps_5m": None} for zone_id in zone_ids]
    empty = {
        "zones": empty_zones,
        "rate_limits": {
            "window_seconds": 300,
            "hits_by_tier": {tier: None for tier in _RATE_LIMIT_TIERS},
        },
    }
    if not url:
        return empty
    try:
        import redis
    except ImportError:
        return empty

    try:
        client = redis.from_url(url, socket_timeout=2)
        client.ping()
        now_min = int(time.time()) // 60
        zones: list[dict[str, Any]] = []
        for zone_id in zone_ids:
            minute_keys = [
                f"nexus:hub:qps:zone:{zone_id}:{now_min - i}" for i in range(5)
            ]
            total = sum(_decode_int(v) for v in client.mget(minute_keys))
            active = client.scard(f"nexus:hub:active:zone:{zone_id}:{now_min}")
            zones.append(
                {
                    "zone_id": zone_id,
                    "clients": int(active),
                    "qps_5m": round(total / 300.0, 2),
                }
            )

        hits_by_tier: dict[str, int] = {}
        for tier in _RATE_LIMIT_TIERS:
            minute_keys = [
                f"nexus:hub:rate_limit:{tier}:{now_min - i}" for i in range(5)
            ]
            hits_by_tier[tier] = sum(_decode_int(v) for v in client.mget(minute_keys))

        return {
            "zones": zones,
            "rate_limits": {"window_seconds": 300, "hits_by_tier": hits_by_tier},
        }
    except Exception:  # noqa: BLE001
        return empty
```

- [ ] **Step 5: Run the Redis detail test**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_read_redis_detail_stats_aggregates_zone_and_rate_limit_counts -v
```

Expected: `PASSED`.

- [ ] **Step 6: Run status tests**

Run:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3874): read hub status detail counters from redis"
```

## Task 4: Write Per-Zone Counters From Audit Middleware

**Files:**
- Modify: `tests/unit/bricks/mcp/test_middleware_audit_metrics.py`
- Modify: `src/nexus/bricks/mcp/middleware_audit.py`

- [ ] **Step 1: Add a failing test for per-zone counter writes**

Add this test after `test_record_metrics_increments_qps_and_sadds_active`:

```python
@pytest.mark.asyncio
async def test_record_metrics_increments_per_zone_counters(monkeypatch):
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379")

    fake_client = AsyncMock()
    fake_client.incr = AsyncMock(return_value=1)
    fake_client.sadd = AsyncMock(return_value=1)
    fake_client.expire = AsyncMock(return_value=True)
    fake_client.close = AsyncMock()

    monkeypatch.setattr(_redis_async, "from_url", lambda _url: fake_client)

    await mw._record_metrics(
        {"subject_id": "kid_abc", "token_hash": "deadbeef", "zone_id": "eng"}
    )

    incr_keys = [call.args[0] for call in fake_client.incr.await_args_list]
    sadd_keys = [call.args[0] for call in fake_client.sadd.await_args_list]

    assert any(key.startswith("nexus:hub:qps:") for key in incr_keys)
    assert any(key.startswith("nexus:hub:qps:zone:eng:") for key in incr_keys)
    assert any(key.startswith("nexus:hub:active:") for key in sadd_keys)
    assert any(key.startswith("nexus:hub:active:zone:eng:") for key in sadd_keys)
    assert fake_client.expire.await_count == 4
```

- [ ] **Step 2: Run the new audit test and verify it fails**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_audit_metrics.py::test_record_metrics_increments_per_zone_counters -v
```

Expected: `FAILED` because only aggregate counters are written.

- [ ] **Step 3: Add per-zone counter writes**

In `src/nexus/bricks/mcp/middleware_audit.py`, inside `_record_metrics`, after the aggregate `active_key` expire call, add:

```python
        zone_id = record.get("zone_id")
        if isinstance(zone_id, str) and zone_id:
            zone_qps_key = f"nexus:hub:qps:zone:{zone_id}:{epoch_min}"
            zone_active_key = f"nexus:hub:active:zone:{zone_id}:{epoch_min}"
            await client.incr(zone_qps_key)
            await client.expire(zone_qps_key, 600)
            _zone_sadd_r = client.sadd(zone_active_key, member)
            if not isinstance(_zone_sadd_r, int):
                await _zone_sadd_r
            await client.expire(zone_active_key, 600)
```

Keep all writes inside the existing `try` block so audit remains fire-and-forget.

- [ ] **Step 4: Run audit middleware tests**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_audit_metrics.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/nexus/bricks/mcp/middleware_audit.py tests/unit/bricks/mcp/test_middleware_audit_metrics.py
git commit -m "feat(#3874): record per-zone hub activity counters"
```

## Task 5: Write Rate-Limit Hit Counters By Tier

**Files:**
- Modify: `tests/unit/bricks/mcp/test_middleware_ratelimit.py`
- Modify: `src/nexus/bricks/mcp/middleware_ratelimit.py`

- [ ] **Step 1: Add a failing test that 429 records the tier**

Add this test after `test_429_response_shape`:

```python
def test_429_records_rate_limit_hit_tier(monkeypatch, app: Starlette) -> None:
    hits: list[str] = []

    async def fake_record(tier: str) -> None:
        hits.append(tier)

    monkeypatch.setattr(
        "nexus.bricks.mcp.middleware_ratelimit._record_rate_limit_hit",
        fake_record,
        raising=False,
    )

    client = TestClient(app)
    for _ in range(3):
        assert client.post("/mcp").status_code == 200
    assert client.post("/mcp").status_code == 429
    assert hits == ["anonymous"]
```

- [ ] **Step 2: Run the new rate-limit test and verify it fails**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py::test_429_records_rate_limit_hit_tier -v
```

Expected: `FAILED` because `_record_rate_limit_hit` is not called.

- [ ] **Step 3: Add the best-effort counter helper**

In `src/nexus/bricks/mcp/middleware_ratelimit.py`, add this helper above `_MCPRateLimitMiddleware`:

```python
async def _record_rate_limit_hit(tier: str) -> None:
    """Best-effort Redis counter for `nexus hub status --detail`."""
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url or url == "memory://":
        return
    try:
        import redis.asyncio as redis
    except ImportError:
        return

    client = redis.from_url(url)
    try:
        epoch_min = int(time.time()) // 60
        key = f"nexus:hub:rate_limit:{tier}:{epoch_min}"
        await client.incr(key)
        await client.expire(key, 600)
    except Exception:  # noqa: BLE001
        return
    finally:
        await client.close()
```

- [ ] **Step 4: Call the helper when rejecting a request**

In `_MCPRateLimitMiddleware.dispatch`, inside the `if not allowed:` branch and before `return JSONResponse(...)`, add:

```python
            await _record_rate_limit_hit(tier)
```

- [ ] **Step 5: Run rate-limit tests**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/nexus/bricks/mcp/middleware_ratelimit.py tests/unit/bricks/mcp/test_middleware_ratelimit.py
git commit -m "feat(#3874): record hub rate-limit hits by tier"
```

## Task 6: Add Local Search Metadata For Detail Output

**Files:**
- Modify: `tests/unit/cli/test_hub.py`
- Modify: `src/nexus/cli/commands/hub.py`

- [ ] **Step 1: Add a failing test for Zoekt index metadata**

Add this test near the status helper tests:

```python
def test_collect_search_detail_reports_zoekt_size_and_latest_mtime(monkeypatch, tmp_path):
    import os
    from datetime import UTC, datetime

    import nexus.cli.commands.hub as hub_module

    base = tmp_path / "zoekt"
    zone_dir = base / "eng"
    zone_dir.mkdir(parents=True)
    first = zone_dir / "one.idx"
    second = zone_dir / "two.idx"
    first.write_bytes(b"a" * 10)
    second.write_bytes(b"b" * 20)
    latest_ts = datetime(2026, 5, 5, 14, 18, tzinfo=UTC).timestamp()
    older_ts = latest_ts - 60
    os.utime(first, (older_ts, older_ts))
    os.utime(second, (latest_ts, latest_ts))

    monkeypatch.setenv("NEXUS_ZOEKT_INDEX_DIR", str(base))

    detail = hub_module._collect_search_detail(["eng", "ops"])

    assert detail["zones"][0]["zone_id"] == "eng"
    assert detail["zones"][0]["zoekt_index_size_bytes"] == 30
    assert detail["zones"][0]["zoekt_index_size_display"] == "30 B"
    assert detail["zones"][0]["zoekt_last_indexed"] == datetime.fromtimestamp(
        latest_ts, tz=UTC
    ).isoformat()
    assert detail["zones"][0]["last_indexed"] == detail["zones"][0]["zoekt_last_indexed"]
    assert detail["zones"][0]["txtai_queue_depth"] is None
    assert detail["zones"][1]["zone_id"] == "ops"
    assert detail["zones"][1]["zoekt_index_size_bytes"] is None
```

- [ ] **Step 2: Run the search metadata test and verify it fails**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_collect_search_detail_reports_zoekt_size_and_latest_mtime -v
```

Expected: `FAILED` because `_collect_search_detail` is still a stub.

- [ ] **Step 3: Import `Path`**

At the top of `src/nexus/cli/commands/hub.py`, add:

```python
from pathlib import Path
```

- [ ] **Step 4: Replace search detail stub with local filesystem implementation**

Replace `_collect_search_detail` with:

```python
def _format_bytes(num_bytes: int | None) -> str | None:
    if num_bytes is None:
        return None
    units = ("B", "KiB", "MiB", "GiB")
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def _zone_index_path(base: Path, zone_id: str) -> Path | None:
    direct = base / zone_id
    if direct.exists():
        return direct
    if not base.exists() or not base.is_dir():
        return None
    matches = sorted(
        child
        for child in base.iterdir()
        if child.name.startswith(f"{zone_id}.") or child.name.startswith(f"{zone_id}-")
    )
    return matches[0] if len(matches) == 1 else None


def _index_path_stats(path: Path) -> tuple[int | None, str | None]:
    try:
        if path.is_file():
            stat = path.stat()
            return stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        total = 0
        latest: float | None = None
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            stat = child.stat()
            total += stat.st_size
            latest = stat.st_mtime if latest is None else max(latest, stat.st_mtime)
        if latest is None:
            return 0, None
        return total, datetime.fromtimestamp(latest, tz=UTC).isoformat()
    except OSError:
        return None, None


def _collect_search_detail(zone_ids: list[str]) -> dict[str, Any]:
    base = Path(os.environ.get("NEXUS_ZOEKT_INDEX_DIR", "/app/data/.zoekt-index"))
    rows: list[dict[str, Any]] = []
    for zone_id in zone_ids:
        zone_path = _zone_index_path(base, zone_id)
        size_bytes: int | None = None
        zoekt_last_indexed: str | None = None
        if zone_path is not None:
            size_bytes, zoekt_last_indexed = _index_path_stats(zone_path)
        rows.append(
            {
                "zone_id": zone_id,
                "zoekt_index_size_bytes": size_bytes,
                "zoekt_index_size_display": _format_bytes(size_bytes),
                "zoekt_last_indexed": zoekt_last_indexed,
                "txtai_queue_depth": None,
                "last_indexed": zoekt_last_indexed,
            }
        )
    return {"zones": rows}
```

- [ ] **Step 5: Run the search metadata test**

Run:

```bash
pytest tests/unit/cli/test_hub.py::test_collect_search_detail_reports_zoekt_size_and_latest_mtime -v
```

Expected: `PASSED`.

- [ ] **Step 6: Run all status tests**

Run:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3874): include local search metadata in hub status detail"
```

## Task 7: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused CLI tests**

Run:

```bash
pytest tests/unit/cli/test_hub.py -v -k status
```

Expected: all selected tests pass.

- [ ] **Step 2: Run audit metrics tests**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_audit_metrics.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Run rate-limit tests**

Run:

```bash
pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Run pre-commit on touched files**

Run:

```bash
pre-commit run --files \
  src/nexus/cli/commands/hub.py \
  src/nexus/bricks/mcp/middleware_audit.py \
  src/nexus/bricks/mcp/middleware_ratelimit.py \
  tests/unit/cli/test_hub.py \
  tests/unit/bricks/mcp/test_middleware_audit_metrics.py \
  tests/unit/bricks/mcp/test_middleware_ratelimit.py
```

Expected: all hooks pass.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git diff --stat HEAD~6..HEAD
git status --short
```

Expected: diff includes only the planned files, and `git status --short` is empty.

## Self-Review Notes

- Spec coverage: the plan covers `--detail`, default output preservation, per-zone Redis counters, per-token `last_used_at`, rate-limit hits by tier, local Zoekt metadata, txtai `n/a`, and fail-soft missing detail data.
- Scope: no migrations, no HTTP status dependency, no Prometheus endpoint, and no remote admin status.
- Type consistency: detail payload uses `zones`, `tokens_detail`, `rate_limits`, and `search` consistently across tests and helpers.
