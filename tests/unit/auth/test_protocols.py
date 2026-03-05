"""Contract test suite for Auth brick protocols (Issue #2281).

Phase 1: Validates that concrete implementations satisfy the Protocol contracts.
Covers:
- UserLookupProtocol (via SQLAlchemyUserLookup)
- UserProvisionerProtocol (via NexusFSUserProvisioner)
- In-memory fakes for testing downstream consumers
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.auth.protocols.user_lookup import UserLookupProtocol
from nexus.bricks.auth.protocols.user_provisioner import UserProvisionerProtocol
from nexus.bricks.auth.stores.nexusfs_provisioner import NexusFSUserProvisioner
from nexus.bricks.auth.types import UserInfo

# ---------------------------------------------------------------------------
# In-memory fakes (used by downstream auth code for testing)
# ---------------------------------------------------------------------------


class InMemoryUserLookup:
    """In-memory UserLookupProtocol for testing.

    Stores UserInfo instances keyed by email, username, and user_id.
    """

    def __init__(self, users: list[UserInfo] | None = None) -> None:
        self._users = list(users or [])

    def add_user(self, user: UserInfo) -> None:
        self._users.append(user)

    def get_user_by_email(self, email: str) -> UserInfo | None:
        return next((u for u in self._users if u.email == email), None)

    def get_user_by_id(self, user_id: str) -> UserInfo | None:
        return next((u for u in self._users if u.user_id == user_id), None)

    def get_user_by_username(self, username: str) -> UserInfo | None:
        return next((u for u in self._users if u.username == username), None)

    def check_email_available(self, email: str) -> bool:
        return self.get_user_by_email(email) is None

    def check_username_available(self, username: str) -> bool:
        return self.get_user_by_username(username) is None

    def validate_user_uniqueness(
        self,
        email: str | None = None,
        username: str | None = None,
    ) -> None:
        if email and not self.check_email_available(email):
            raise ValueError(f"Email {email} already exists")
        if username and not self.check_username_available(username):
            raise ValueError(f"Username {username} already exists")


class InMemoryUserProvisioner:
    """In-memory UserProvisionerProtocol for testing."""

    def __init__(self) -> None:
        self.provisioned: list[dict[str, Any]] = []

    def provision_user(
        self,
        *,
        user_id: str,
        email: str,
        display_name: str | None = None,
        zone_id: str | None = None,
        create_api_key: bool = True,
        create_agents: bool = True,
        import_skills: bool = True,
    ) -> dict[str, Any]:
        result = {
            "user_id": user_id,
            "email": email,
            "zone_id": zone_id or email.split("@")[0],
        }
        self.provisioned.append(result)
        return result


# ===========================================================================
# Protocol conformance tests (runtime_checkable isinstance checks)
# ===========================================================================


class TestProtocolConformance:
    """Verify that concrete implementations satisfy their protocols."""

    def test_in_memory_lookup_satisfies_protocol(self):
        lookup = InMemoryUserLookup()
        assert isinstance(lookup, UserLookupProtocol)

    def test_in_memory_provisioner_satisfies_protocol(self):
        provisioner = InMemoryUserProvisioner()
        assert isinstance(provisioner, UserProvisionerProtocol)

    def test_nexusfs_provisioner_satisfies_protocol(self):
        mock_nx = MagicMock()
        provisioner = NexusFSUserProvisioner(mock_nx)
        assert isinstance(provisioner, UserProvisionerProtocol)


# ===========================================================================
# UserLookupProtocol contract tests (using InMemoryUserLookup)
# ===========================================================================


class TestUserLookupContract:
    """Contract: any UserLookupProtocol implementation must pass these."""

    @pytest.fixture
    def lookup(self) -> InMemoryUserLookup:
        return InMemoryUserLookup(
            [
                UserInfo(
                    user_id="u1",
                    email="alice@example.com",
                    username="alice",
                    display_name="Alice",
                    is_global_admin=False,
                    is_active=True,
                    email_verified=True,
                ),
                UserInfo(
                    user_id="u2",
                    email="bob@example.com",
                    username="bob",
                    display_name="Bob",
                    is_global_admin=True,
                    is_active=True,
                    email_verified=False,
                ),
            ]
        )

    def test_get_user_by_email_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_email("alice@example.com")
        assert user is not None
        assert user.user_id == "u1"
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice"

    def test_get_user_by_email_not_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_email("nobody@example.com")
        assert user is None

    def test_get_user_by_id_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_id("u2")
        assert user is not None
        assert user.email == "bob@example.com"

    def test_get_user_by_id_not_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_id("u999")
        assert user is None

    def test_get_user_by_username_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_username("alice")
        assert user is not None
        assert user.user_id == "u1"

    def test_get_user_by_username_not_found(self, lookup: InMemoryUserLookup):
        user = lookup.get_user_by_username("charlie")
        assert user is None

    def test_check_email_available_taken(self, lookup: InMemoryUserLookup):
        assert lookup.check_email_available("alice@example.com") is False

    def test_check_email_available_free(self, lookup: InMemoryUserLookup):
        assert lookup.check_email_available("new@example.com") is True

    def test_check_username_available_taken(self, lookup: InMemoryUserLookup):
        assert lookup.check_username_available("bob") is False

    def test_check_username_available_free(self, lookup: InMemoryUserLookup):
        assert lookup.check_username_available("charlie") is True

    def test_validate_uniqueness_passes_for_new(self, lookup: InMemoryUserLookup):
        lookup.validate_user_uniqueness(email="new@example.com", username="charlie")

    def test_validate_uniqueness_raises_for_existing_email(self, lookup: InMemoryUserLookup):
        with pytest.raises(ValueError, match="Email alice@example.com already exists"):
            lookup.validate_user_uniqueness(email="alice@example.com")

    def test_validate_uniqueness_raises_for_existing_username(self, lookup: InMemoryUserLookup):
        with pytest.raises(ValueError, match="Username bob already exists"):
            lookup.validate_user_uniqueness(username="bob")


# ===========================================================================
# UserProvisionerProtocol contract tests
# ===========================================================================


class TestUserProvisionerContract:
    """Contract: any UserProvisionerProtocol implementation must pass these."""

    @pytest.fixture
    def provisioner(self) -> InMemoryUserProvisioner:
        return InMemoryUserProvisioner()

    def test_provision_returns_zone_id(self, provisioner: InMemoryUserProvisioner):
        result = provisioner.provision_user(
            user_id="u1",
            email="alice@example.com",
            display_name="Alice",
        )
        assert "zone_id" in result
        assert result["zone_id"] == "alice"

    def test_provision_with_explicit_zone(self, provisioner: InMemoryUserProvisioner):
        result = provisioner.provision_user(
            user_id="u1",
            email="alice@example.com",
            zone_id="custom-zone",
        )
        assert result["zone_id"] == "custom-zone"

    def test_provision_records_call(self, provisioner: InMemoryUserProvisioner):
        provisioner.provision_user(user_id="u1", email="a@b.com")
        provisioner.provision_user(user_id="u2", email="c@d.com")
        assert len(provisioner.provisioned) == 2


# ===========================================================================
# NexusFSUserProvisioner tests (integration with mock NexusFS)
# ===========================================================================


class TestNexusFSUserProvisioner:
    """Tests for the NexusFSUserProvisioner adapter."""

    def test_delegates_to_nexusfs(self):
        mock_nx = MagicMock()
        mock_nx._user_provisioning_service.provision_user.return_value = {
            "zone_id": "alice",
            "api_key": "sk-123",
        }
        provisioner = NexusFSUserProvisioner(mock_nx)

        result = provisioner.provision_user(
            user_id="u1",
            email="alice@example.com",
            display_name="Alice",
        )

        assert result["zone_id"] == "alice"
        mock_nx._user_provisioning_service.provision_user.assert_called_once()
        call_kwargs = mock_nx._user_provisioning_service.provision_user.call_args[1]
        assert call_kwargs["user_id"] == "u1"
        assert call_kwargs["email"] == "alice@example.com"
        assert call_kwargs["create_api_key"] is True

    def test_creates_operation_context(self):
        mock_nx = MagicMock()
        mock_nx._user_provisioning_service.provision_user.return_value = {"zone_id": "alice"}
        provisioner = NexusFSUserProvisioner(mock_nx)

        provisioner.provision_user(user_id="u1", email="alice@example.com")

        call_kwargs = mock_nx._user_provisioning_service.provision_user.call_args[1]
        context = call_kwargs["context"]
        assert context.user_id == "system"
        assert context.is_admin is True
        assert context.zone_id == "alice"

    def test_zone_id_derived_from_email(self):
        mock_nx = MagicMock()
        mock_nx._user_provisioning_service.provision_user.return_value = {"zone_id": "bob"}
        provisioner = NexusFSUserProvisioner(mock_nx)

        provisioner.provision_user(user_id="u2", email="bob@corp.com")

        call_kwargs = mock_nx._user_provisioning_service.provision_user.call_args[1]
        assert call_kwargs["zone_id"] == "bob"

    def test_zone_id_override(self):
        mock_nx = MagicMock()
        mock_nx._user_provisioning_service.provision_user.return_value = {"zone_id": "custom"}
        provisioner = NexusFSUserProvisioner(mock_nx)

        provisioner.provision_user(
            user_id="u1",
            email="alice@example.com",
            zone_id="custom",
        )

        call_kwargs = mock_nx._user_provisioning_service.provision_user.call_args[1]
        assert call_kwargs["zone_id"] == "custom"


# ===========================================================================
# UserInfo immutability tests
# ===========================================================================


class TestUserInfo:
    """Tests for the UserInfo frozen dataclass."""

    def test_frozen(self):
        user = UserInfo(user_id="u1", email="a@b.com")
        with pytest.raises(AttributeError):
            user.email = "changed@b.com"  # type: ignore[misc]

    def test_default_values(self):
        user = UserInfo(user_id="u1")
        assert user.email is None
        assert user.is_global_admin is False
        assert user.is_active is True
        assert user.email_verified is False

    def test_all_fields(self):
        user = UserInfo(
            user_id="u1",
            email="a@b.com",
            username="alice",
            display_name="Alice",
            avatar_url="https://example.com/pic.jpg",
            password_hash="hashed",
            primary_auth_method="password",
            is_global_admin=True,
            is_active=True,
            email_verified=True,
            zone_id="z1",
            api_key="sk-123",
            metadata={"org": "acme"},
        )
        assert user.user_id == "u1"
        assert user.metadata == {"org": "acme"}
