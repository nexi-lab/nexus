"""Unit tests for PasswordVaultService.

The service is a thin JSON-serialisation wrapper over SecretsService —
these tests mock SecretsService and verify:
    * VaultEntry ↔ JSON round-trips via put/get
    * list_entries uses list_secrets + batch_get and hydrates entries
    * delete / restore / list_versions delegate with the right namespace
    * Missing entries raise VaultEntryNotFoundError on get_entry
    * Malformed persisted JSON is skipped (not fatal) in list_entries
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.secrets_access import AccessAuditContext
from nexus.services.password_vault.schema import VaultEntry
from nexus.services.password_vault.service import (
    PasswordVaultService,
    VaultEntryNotFoundError,
)


@pytest.fixture()
def secrets() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def vault(secrets: MagicMock) -> PasswordVaultService:
    return PasswordVaultService(secrets_service=secrets)


def _sample_entry(**overrides: Any) -> VaultEntry:
    base = {
        "title": "github",
        "username": "alice",
        "password": "hunter2",
        "url": "https://github.com",
        "notes": "primary work account",
        "tags": "dev,work",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "extra": {"recovery_codes": ["a1", "b2"]},
    }
    base.update(overrides)
    return VaultEntry.model_validate(base)


# ---------------------------------------------------------------------------
# put_entry
# ---------------------------------------------------------------------------


class TestPutEntry:
    def test_serialises_entry_to_json_under_passwords_namespace(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        entry = _sample_entry()
        secrets.put_secret.return_value = {
            "id": 42,
            "version": 1,
            "created_at": "2026-04-20T10:00:00",
        }

        result = vault.put_entry(entry, actor_id="alice", subject_id="alice", subject_type="user")

        secrets.put_secret.assert_called_once()
        kwargs = secrets.put_secret.call_args.kwargs
        assert kwargs["namespace"] == "passwords"
        assert kwargs["key"] == "github"
        assert kwargs["actor_id"] == "alice"
        assert kwargs["subject_id"] == "alice"
        assert kwargs["subject_type"] == "user"

        # Value is JSON and round-trips to the same entry
        payload = json.loads(kwargs["value"])
        assert VaultEntry.model_validate(payload) == entry

        assert result == {
            "id": 42,
            "title": "github",
            "version": 1,
            "created_at": "2026-04-20T10:00:00",
        }

    def test_accepts_minimal_entry(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        entry = VaultEntry(title="wifi-home")
        secrets.put_secret.return_value = {"id": 1, "version": 1, "created_at": None}

        vault.put_entry(entry)

        kwargs = secrets.put_secret.call_args.kwargs
        payload = json.loads(kwargs["value"])
        assert payload["title"] == "wifi-home"
        assert payload["password"] is None


# ---------------------------------------------------------------------------
# get_entry
# ---------------------------------------------------------------------------


class TestGetEntry:
    def test_decodes_json_to_vault_entry(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        entry = _sample_entry()
        secrets.get_secret.return_value = {
            "value": json.dumps(entry.model_dump()),
            "version": 3,
        }

        got = vault.get_entry("github", version=3, actor_id="alice")

        assert got == entry
        kwargs = secrets.get_secret.call_args.kwargs
        assert kwargs["namespace"] == "passwords"
        assert kwargs["key"] == "github"
        assert kwargs["version"] == 3

    def test_missing_raises_not_found(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        secrets.get_secret.return_value = None

        with pytest.raises(VaultEntryNotFoundError):
            vault.get_entry("nonexistent")

    def test_passes_version_kwarg_through(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        secrets.get_secret.return_value = {
            "value": json.dumps({"title": "x"}),
            "version": 7,
        }

        vault.get_entry("x", version=7)

        assert secrets.get_secret.call_args.kwargs["version"] == 7


# ---------------------------------------------------------------------------
# list_entries
# ---------------------------------------------------------------------------


class TestListEntries:
    def test_empty_when_no_metadata(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        secrets.list_secrets.return_value = []

        assert vault.list_entries() == []
        secrets.batch_get.assert_not_called()

    def test_hydrates_all_entries_via_batch_get(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        e1 = _sample_entry(title="github")
        e2 = _sample_entry(title="aws", username="root", password="p@ss")
        secrets.list_secrets.return_value = [
            {"key": "github", "namespace": "passwords"},
            {"key": "aws", "namespace": "passwords"},
        ]
        secrets.batch_get.return_value = {
            "passwords:github": json.dumps(e1.model_dump()),
            "passwords:aws": json.dumps(e2.model_dump()),
        }

        entries = vault.list_entries(actor_id="alice")

        assert entries == [e1, e2]
        queries = secrets.batch_get.call_args.kwargs["queries"]
        assert queries == [
            {"namespace": "passwords", "key": "github"},
            {"namespace": "passwords", "key": "aws"},
        ]

    def test_skips_keys_missing_from_batch_get(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        e1 = _sample_entry(title="github")
        secrets.list_secrets.return_value = [
            {"key": "github", "namespace": "passwords"},
            {"key": "disabled", "namespace": "passwords"},
        ]
        # Only github comes back from batch_get (disabled secret)
        secrets.batch_get.return_value = {
            "passwords:github": json.dumps(e1.model_dump()),
        }

        assert vault.list_entries() == [e1]

    def test_skips_malformed_json(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        e1 = _sample_entry(title="good")
        secrets.list_secrets.return_value = [
            {"key": "good", "namespace": "passwords"},
            {"key": "broken", "namespace": "passwords"},
        ]
        secrets.batch_get.return_value = {
            "passwords:good": json.dumps(e1.model_dump()),
            "passwords:broken": "not-json-at-all",
        }

        assert vault.list_entries() == [e1]


# ---------------------------------------------------------------------------
# delete_entry / restore_entry / list_versions
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_delete_entry_delegates(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        secrets.delete_secret.return_value = True

        assert vault.delete_entry("github", actor_id="alice") is True
        kwargs = secrets.delete_secret.call_args.kwargs
        assert kwargs["namespace"] == "passwords"
        assert kwargs["key"] == "github"
        assert kwargs["actor_id"] == "alice"

    def test_restore_entry_delegates(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        secrets.restore_secret.return_value = True

        assert vault.restore_entry("github") is True
        assert secrets.restore_secret.call_args.kwargs["key"] == "github"

    def test_list_versions_delegates(self, vault: PasswordVaultService, secrets: MagicMock) -> None:
        secrets.list_versions.return_value = [
            {"version": 3, "created_at": "2026-04-20T10:00:00"},
            {"version": 2, "created_at": "2026-04-19T10:00:00"},
            {"version": 1, "created_at": "2026-04-18T10:00:00"},
        ]

        versions = vault.list_versions("github", subject_id="alice", subject_type="user")

        assert len(versions) == 3
        assert versions[0]["version"] == 3
        kwargs = secrets.list_versions.call_args.kwargs
        assert kwargs["namespace"] == "passwords"
        assert kwargs["key"] == "github"
        assert kwargs["subject_id"] == "alice"


# ---------------------------------------------------------------------------
# AccessAuditContext propagation
# ---------------------------------------------------------------------------


class TestAuditContextPropagation:
    def test_get_entry_forwards_audit_context(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        secrets.get_secret.return_value = {
            "value": json.dumps({"title": "github"}),
            "version": 1,
        }
        ctx = AccessAuditContext(
            access_context="auto_login", client_id="sudowork", agent_session="s-42"
        )

        vault.get_entry("github", audit_context=ctx)

        assert secrets.get_secret.call_args.kwargs["audit_context"] is ctx

    def test_get_entry_without_audit_context_passes_none(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        secrets.get_secret.return_value = {
            "value": json.dumps({"title": "x"}),
            "version": 1,
        }

        vault.get_entry("x")

        assert secrets.get_secret.call_args.kwargs["audit_context"] is None

    def test_list_entries_forwards_audit_context_to_batch_get(
        self, vault: PasswordVaultService, secrets: MagicMock
    ) -> None:
        secrets.list_secrets.return_value = [{"key": "github", "namespace": "passwords"}]
        secrets.batch_get.return_value = {
            "passwords:github": json.dumps({"title": "github"}),
        }
        ctx = AccessAuditContext(access_context="reveal_approved")

        vault.list_entries(audit_context=ctx)

        assert secrets.batch_get.call_args.kwargs["audit_context"] is ctx


# ---------------------------------------------------------------------------
# AccessAuditContext value object
# ---------------------------------------------------------------------------


class TestAccessAuditContext:
    def test_default_access_context_is_admin_cli(self) -> None:
        ctx = AccessAuditContext()

        assert ctx.access_context == "admin_cli"
        assert ctx.to_audit_details() == {"access_context": "admin_cli"}

    def test_to_audit_details_omits_none_fields(self) -> None:
        ctx = AccessAuditContext(access_context="auto_login")

        assert ctx.to_audit_details() == {"access_context": "auto_login"}

    def test_to_audit_details_includes_all_when_set(self) -> None:
        ctx = AccessAuditContext(
            access_context="reveal_approved",
            client_id="sudowork-ui",
            agent_session="abc-123",
        )

        assert ctx.to_audit_details() == {
            "access_context": "reveal_approved",
            "client_id": "sudowork-ui",
            "agent_session": "abc-123",
        }
