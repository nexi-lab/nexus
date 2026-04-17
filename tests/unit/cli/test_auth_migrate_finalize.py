from __future__ import annotations

from click.testing import CliRunner

from nexus.bricks.auth.cli_commands import auth


class _StubService:
    """Minimal stub matching what `migrate --finalize` needs."""

    def __init__(self, legacy_rows, profile_rows, failing_keys=()):
        self._legacy = _StubLegacy(legacy_rows)
        self._profiles = _StubProfiles(profile_rows)
        self._backend = _StubBackend(failing_keys=failing_keys)

    def migration_components(self):
        return self._legacy, self._profiles, self._backend


class _StubLegacy:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.deleted: list[str] = []

    def list_rows(self):
        return list(self.rows.items())

    def delete(self, pid):
        self.deleted.append(pid)
        del self.rows[pid]


class _StubProfiles:
    def __init__(self, rows):
        # rows: iterable of profile_id strings (we only need .get(pid) returning truthy)
        self._profiles = {pid: object() for pid in rows}

    def get(self, pid):
        return self._profiles.get(pid)


class _StubBackend:
    def __init__(self, failing_keys):
        self.failing = set(failing_keys)

    async def health_check(self, key):
        from nexus.bricks.auth.credential_backend import BackendHealth, HealthStatus

        status = HealthStatus.UNHEALTHY if key in self.failing else HealthStatus.HEALTHY
        msg = "probe failed" if key in self.failing else "ok"
        return BackendHealth(status=status, message=msg)


def test_migrate_finalize_happy_path(monkeypatch):
    service = _StubService(
        legacy_rows={"openai/team": "sk-x", "anthropic/team": "sk-y"},
        profile_rows={"openai/team", "anthropic/team"},
    )
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["migrate", "--finalize"])
    assert result.exit_code == 0, result.output
    assert "finalized" in result.output.lower() or "deleted" in result.output.lower()


def test_migrate_finalize_missing_profile_unhappy(monkeypatch):
    service = _StubService(
        legacy_rows={"openai/team": "sk-x", "orphan/team": "sk-z"},
        profile_rows={"openai/team"},
    )
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["migrate", "--finalize"])
    assert result.exit_code == 1
    assert "orphan/team" in result.output


def test_migrate_finalize_mutually_exclusive_with_apply(monkeypatch):
    service = _StubService(legacy_rows={}, profile_rows=set())
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["migrate", "--apply", "--finalize"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()
