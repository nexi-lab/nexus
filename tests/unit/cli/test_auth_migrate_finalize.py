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


def test_post_finalize_full_auth_flow_without_tokens_db(monkeypatch, tmp_path):
    """After finalize: list, doctor, test, disconnect all work with no legacy tokens.db.

    This asserts the Phase 4 promise: the profile store is authoritative, and
    the absence of the legacy tokens.db does not break any CLI command.
    """
    from nexus.bricks.auth.tests.helpers import build_unified_service_for_tests

    # Build a service backed purely by the profile store — no legacy token DB.
    # The helper uses a tmp FileSecretCredentialStore and oauth_service=None.
    service = build_unified_service_for_tests(tmp_path)

    # Seed one credential directly into the secret store (no legacy path).
    # s3 is the canonical secret-backed service; supply the two required fields.
    service.connect_secret(
        "s3", {"access_key_id": "AKIA_POST_FINALIZE", "secret_access_key": "sk-post-finalize"}
    )

    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()

    # --- list ---
    list_result = runner.invoke(auth, ["list"])
    assert list_result.exit_code == 0, list_result.output
    assert "s3" in list_result.output

    # --- doctor ---
    # doctor returns 0 when no failures, 1 otherwise. Either is acceptable —
    # the key claim is it ran without crashing.
    doctor_result = runner.invoke(auth, ["doctor"])
    assert doctor_result.exit_code in (0, 1), doctor_result.output

    # --- test ---
    # test may succeed or fail depending on credential shape, but must not crash.
    test_result = runner.invoke(auth, ["test", "s3"])
    assert test_result.exit_code in (0, 1), test_result.output

    # --- disconnect ---
    disconnect_result = runner.invoke(auth, ["disconnect", "s3"])
    assert disconnect_result.exit_code == 0, disconnect_result.output

    # Verify no tokens.db was created anywhere under tmp_path — the secret store's
    # backing file is credentials.json, not tokens.db.
    leaked_tokens = list(tmp_path.rglob("tokens.db"))
    assert leaked_tokens == [], f"legacy tokens.db should not appear: {leaked_tokens}"
