from __future__ import annotations

from nexus.bricks.auth.migrate import FinalizeResult, OldStoreAdapter, finalize_migration


class _FakeLegacyStore:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.deleted: list[str] = []

    def list_rows(self):
        return list(self.rows.items())

    def delete(self, profile_id):
        self.deleted.append(profile_id)
        del self.rows[profile_id]


class _FakeProfileStore:
    def __init__(self, profiles):
        self._profiles = {p.id: p for p in profiles}

    def get(self, profile_id):
        return self._profiles.get(profile_id)


class _FakeBackend:
    def __init__(self, failing_keys=()):
        self.failing = set(failing_keys)

    async def health_check(self, backend_key):
        from nexus.bricks.auth.credential_backend import BackendHealth, HealthStatus

        if backend_key in self.failing:
            return BackendHealth(status=HealthStatus.UNHEALTHY, message="probe failed")
        return BackendHealth(status=HealthStatus.HEALTHY, message="ok")


def _make_profile(profile_id: str):
    """Build a minimal AuthProfile matching whatever the real dataclass requires."""
    from nexus.bricks.auth.profile import AuthProfile

    # AuthProfile requires: id, provider, account_identifier, backend, backend_key
    parts = profile_id.split("/", 1)
    provider = parts[0]
    account_identifier = parts[1] if len(parts) > 1 else profile_id
    return AuthProfile(
        id=profile_id,
        provider=provider,
        account_identifier=account_identifier,
        backend="nexus-token-manager",
        backend_key=profile_id,
    )


def test_finalize_happy_path_deletes_all_legacy_rows():
    legacy = _FakeLegacyStore({"openai/team": "sk-x", "anthropic/team": "sk-y"})
    profiles = _FakeProfileStore([_make_profile("openai/team"), _make_profile("anthropic/team")])
    backend = _FakeBackend()

    result = finalize_migration(
        legacy_store=legacy,
        profile_store=profiles,
        backend=backend,
    )
    assert isinstance(result, FinalizeResult)
    assert result.ok
    assert set(result.deleted) == {"openai/team", "anthropic/team"}
    assert legacy.rows == {}


def test_finalize_unhappy_missing_profile_aborts_without_delete():
    legacy = _FakeLegacyStore({"openai/team": "sk-x", "orphan/team": "sk-z"})
    profiles = _FakeProfileStore([_make_profile("openai/team")])
    backend = _FakeBackend()

    result = finalize_migration(
        legacy_store=legacy,
        profile_store=profiles,
        backend=backend,
    )
    assert not result.ok
    assert result.failures
    assert any("orphan/team" in f.detail for f in result.failures)
    assert legacy.deleted == []


def test_finalize_unhappy_health_check_failure_aborts_without_delete():
    legacy = _FakeLegacyStore({"openai/team": "sk-x"})
    profiles = _FakeProfileStore([_make_profile("openai/team")])
    backend = _FakeBackend(failing_keys={"openai/team"})

    result = finalize_migration(
        legacy_store=legacy,
        profile_store=profiles,
        backend=backend,
    )
    assert not result.ok
    assert result.failures
    assert legacy.deleted == []


# ---------------------------------------------------------------------------
# OldStoreAdapter.delete() persists to the real credential store (#3741)
# ---------------------------------------------------------------------------


class _FakeOAuthService:
    """Minimal stub for OAuthCredentialService used in finalize tests."""

    def __init__(self):
        self.deleted_calls: list[dict] = []

    async def delete_credentials(
        self,
        provider: str,
        user_email: str,
        zone_id: str | None = None,
    ) -> bool:
        self.deleted_calls.append(
            {"provider": provider, "user_email": user_email, "zone_id": zone_id}
        )
        return True


def test_old_store_adapter_delete_calls_oauth_service():
    """OldStoreAdapter.delete() with an oauth_service calls delete_credentials (#3741)."""
    fake_service = _FakeOAuthService()
    creds = [{"provider": "openai", "user_email": "alice@example.com", "zone_id": "root"}]
    adapter = OldStoreAdapter(creds, oauth_service=fake_service)

    assert "openai/alice@example.com" in {pid for pid, _ in adapter.list_rows()}

    adapter.delete("openai/alice@example.com")

    # In-memory snapshot was cleared.
    assert adapter.list_rows() == []

    # Real credential service was invoked.
    assert len(fake_service.deleted_calls) == 1
    call = fake_service.deleted_calls[0]
    assert call["provider"] == "openai"
    assert call["user_email"] == "alice@example.com"


def test_old_store_adapter_delete_without_oauth_service_only_clears_snapshot():
    """OldStoreAdapter.delete() without oauth_service only removes the in-memory row."""
    creds = [{"provider": "openai", "user_email": "bob@example.com"}]
    adapter = OldStoreAdapter(creds)  # no oauth_service

    adapter.delete("openai/bob@example.com")

    # In-memory snapshot was cleared — no error raised.
    assert adapter.list_rows() == []
