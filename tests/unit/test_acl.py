"""Unit tests for Access Control Lists (ACL)."""

import pytest

from nexus.core.acl import (
    ACL,
    ACLEntry,
    ACLEntryType,
    ACLManager,
    ACLPermission,
)


class TestACLPermission:
    """Test ACLPermission enum."""

    def test_permission_values(self):
        """Test permission values."""
        assert ACLPermission.READ == "read"
        assert ACLPermission.WRITE == "write"
        assert ACLPermission.EXECUTE == "execute"


class TestACLEntryType:
    """Test ACLEntryType enum."""

    def test_entry_type_values(self):
        """Test entry type values."""
        assert ACLEntryType.USER == "user"
        assert ACLEntryType.GROUP == "group"
        assert ACLEntryType.MASK == "mask"
        assert ACLEntryType.OTHER == "other"


class TestACLEntry:
    """Test ACLEntry class."""

    def test_init_user_entry(self):
        """Test user entry initialization."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ, ACLPermission.WRITE},
        )
        assert entry.entry_type == ACLEntryType.USER
        assert entry.identifier == "alice"
        assert ACLPermission.READ in entry.permissions
        assert ACLPermission.WRITE in entry.permissions
        assert ACLPermission.EXECUTE not in entry.permissions
        assert not entry.deny

    def test_init_group_entry(self):
        """Test group entry initialization."""
        entry = ACLEntry(
            entry_type=ACLEntryType.GROUP,
            identifier="developers",
            permissions={ACLPermission.READ},
        )
        assert entry.entry_type == ACLEntryType.GROUP
        assert entry.identifier == "developers"
        assert ACLPermission.READ in entry.permissions

    def test_init_deny_entry(self):
        """Test deny entry initialization."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="bob",
            permissions=set(),
            deny=True,
        )
        assert entry.deny

    def test_init_user_without_identifier(self):
        """Test user entry requires identifier."""
        with pytest.raises(ValueError, match="entry requires an identifier"):
            ACLEntry(
                entry_type=ACLEntryType.USER,
                identifier=None,
                permissions={ACLPermission.READ},
            )

    def test_init_mask_with_identifier(self):
        """Test mask entry cannot have identifier."""
        with pytest.raises(ValueError, match="entry cannot have identifier"):
            ACLEntry(
                entry_type=ACLEntryType.MASK,
                identifier="someone",
                permissions={ACLPermission.READ},
            )

    def test_has_permission(self):
        """Test has_permission method."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ, ACLPermission.WRITE},
        )
        assert entry.has_permission(ACLPermission.READ)
        assert entry.has_permission(ACLPermission.WRITE)
        assert not entry.has_permission(ACLPermission.EXECUTE)

    def test_to_string_user(self):
        """Test converting user entry to string."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ, ACLPermission.WRITE},
        )
        assert entry.to_string() == "user:alice:rw-"

    def test_to_string_group(self):
        """Test converting group entry to string."""
        entry = ACLEntry(
            entry_type=ACLEntryType.GROUP,
            identifier="developers",
            permissions={ACLPermission.READ, ACLPermission.EXECUTE},
        )
        assert entry.to_string() == "group:developers:r-x"

    def test_to_string_deny(self):
        """Test converting deny entry to string."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="bob",
            permissions=set(),
            deny=True,
        )
        assert entry.to_string() == "deny:user:bob:---"

    def test_from_string_user(self):
        """Test parsing user entry from string."""
        entry = ACLEntry.from_string("user:alice:rw-")
        assert entry.entry_type == ACLEntryType.USER
        assert entry.identifier == "alice"
        assert ACLPermission.READ in entry.permissions
        assert ACLPermission.WRITE in entry.permissions
        assert ACLPermission.EXECUTE not in entry.permissions
        assert not entry.deny

    def test_from_string_group(self):
        """Test parsing group entry from string."""
        entry = ACLEntry.from_string("group:developers:r-x")
        assert entry.entry_type == ACLEntryType.GROUP
        assert entry.identifier == "developers"
        assert ACLPermission.READ in entry.permissions
        assert ACLPermission.EXECUTE in entry.permissions

    def test_from_string_deny(self):
        """Test parsing deny entry from string."""
        entry = ACLEntry.from_string("deny:user:bob:---")
        assert entry.entry_type == ACLEntryType.USER
        assert entry.identifier == "bob"
        assert entry.deny

    def test_from_string_invalid_format(self):
        """Test parsing invalid format."""
        with pytest.raises(ValueError, match="invalid ACL entry"):
            ACLEntry.from_string("invalid")

    def test_from_string_invalid_type(self):
        """Test parsing invalid entry type."""
        with pytest.raises(ValueError, match="invalid ACL entry type"):
            ACLEntry.from_string("invalid:alice:rw-")

    def test_from_string_invalid_permissions(self):
        """Test parsing invalid permissions."""
        with pytest.raises(ValueError, match="permission string must be 3 characters"):
            ACLEntry.from_string("user:alice:rwx-")  # Too long

    def test_repr(self):
        """Test entry repr."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ},
        )
        assert repr(entry) == "ACLEntry('user:alice:r--')"

    def test_str(self):
        """Test entry str."""
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ},
        )
        assert str(entry) == "user:alice:r--"


class TestACL:
    """Test ACL class."""

    def test_init_empty(self):
        """Test empty ACL initialization."""
        acl = ACL(entries=[])
        assert len(acl.entries) == 0

    def test_init_with_entries(self):
        """Test ACL initialization with entries."""
        entries = [
            ACLEntry(
                entry_type=ACLEntryType.USER,
                identifier="alice",
                permissions={ACLPermission.READ},
            ),
            ACLEntry(
                entry_type=ACLEntryType.GROUP,
                identifier="developers",
                permissions={ACLPermission.READ, ACLPermission.WRITE},
            ),
        ]
        acl = ACL(entries=entries)
        assert len(acl.entries) == 2

    def test_check_permission_user_allow(self):
        """Test user allowed by ACL."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                )
            ]
        )
        assert acl.check_permission("alice", [], ACLPermission.READ) is True

    def test_check_permission_user_deny(self):
        """Test user denied by ACL."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="bob",
                    permissions=set(),
                    deny=True,
                )
            ]
        )
        assert acl.check_permission("bob", [], ACLPermission.READ) is False

    def test_check_permission_group_allow(self):
        """Test group allowed by ACL."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.GROUP,
                    identifier="developers",
                    permissions={ACLPermission.READ, ACLPermission.WRITE},
                )
            ]
        )
        assert acl.check_permission("alice", ["developers"], ACLPermission.READ) is True
        assert acl.check_permission("alice", ["developers"], ACLPermission.WRITE) is True

    def test_check_permission_no_match(self):
        """Test no ACL match returns None."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                )
            ]
        )
        # Bob not in ACL
        assert acl.check_permission("bob", [], ACLPermission.READ) is None

    def test_check_permission_deny_priority(self):
        """Test deny entries have priority."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions=set(),
                    deny=True,
                ),
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                ),
            ]
        )
        # Deny comes first, so alice is denied
        assert acl.check_permission("alice", [], ACLPermission.READ) is False

    def test_add_entry(self):
        """Test adding ACL entry."""
        acl = ACL(entries=[])
        entry = ACLEntry(
            entry_type=ACLEntryType.USER,
            identifier="alice",
            permissions={ACLPermission.READ},
        )
        acl.add_entry(entry)
        assert len(acl.entries) == 1

    def test_remove_entry(self):
        """Test removing ACL entry."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                ),
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="bob",
                    permissions={ACLPermission.WRITE},
                ),
            ]
        )
        assert acl.remove_entry(ACLEntryType.USER, "alice") is True
        assert len(acl.entries) == 1
        assert acl.entries[0].identifier == "bob"

    def test_remove_entry_not_found(self):
        """Test removing non-existent entry."""
        acl = ACL(entries=[])
        assert acl.remove_entry(ACLEntryType.USER, "alice") is False

    def test_get_entries(self):
        """Test getting ACL entries."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                ),
                ACLEntry(
                    entry_type=ACLEntryType.GROUP,
                    identifier="developers",
                    permissions={ACLPermission.WRITE},
                ),
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="bob",
                    permissions={ACLPermission.EXECUTE},
                ),
            ]
        )
        # Get all user entries
        user_entries = acl.get_entries(entry_type=ACLEntryType.USER)
        assert len(user_entries) == 2

        # Get specific user
        alice_entries = acl.get_entries(entry_type=ACLEntryType.USER, identifier="alice")
        assert len(alice_entries) == 1
        assert alice_entries[0].identifier == "alice"

    def test_to_strings(self):
        """Test converting ACL to strings."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                ),
                ACLEntry(
                    entry_type=ACLEntryType.GROUP,
                    identifier="developers",
                    permissions={ACLPermission.WRITE},
                ),
            ]
        )
        strings = acl.to_strings()
        assert len(strings) == 2
        assert "user:alice:r--" in strings
        assert "group:developers:-w-" in strings

    def test_from_strings(self):
        """Test parsing ACL from strings."""
        strings = ["user:alice:r--", "group:developers:-w-"]
        acl = ACL.from_strings(strings)
        assert len(acl.entries) == 2
        assert acl.entries[0].identifier == "alice"
        assert acl.entries[1].identifier == "developers"

    def test_empty(self):
        """Test creating empty ACL."""
        acl = ACL.empty()
        assert len(acl.entries) == 0

    def test_repr(self):
        """Test ACL repr."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                )
            ]
        )
        assert repr(acl) == "ACL(['user:alice:r--'])"

    def test_str(self):
        """Test ACL str."""
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                ),
                ACLEntry(
                    entry_type=ACLEntryType.GROUP,
                    identifier="developers",
                    permissions={ACLPermission.WRITE},
                ),
            ]
        )
        assert str(acl) == "user:alice:r--\ngroup:developers:-w-"


class TestACLManager:
    """Test ACLManager class."""

    def test_init(self):
        """Test manager initialization."""
        manager = ACLManager()
        assert manager is not None

    def test_grant_user(self):
        """Test granting user permissions."""
        manager = ACLManager()
        acl = ACL.empty()

        manager.grant_user(acl, "alice", read=True, write=True)
        assert len(acl.entries) == 1
        assert acl.check_permission("alice", [], ACLPermission.READ) is True
        assert acl.check_permission("alice", [], ACLPermission.WRITE) is True

    def test_grant_user_overwrites(self):
        """Test granting user permissions overwrites existing."""
        manager = ACLManager()
        acl = ACL.empty()

        # Grant read
        manager.grant_user(acl, "alice", read=True)
        assert len(acl.entries) == 1

        # Grant write (should overwrite)
        manager.grant_user(acl, "alice", write=True)
        assert len(acl.entries) == 1
        assert acl.check_permission("alice", [], ACLPermission.READ) is None
        assert acl.check_permission("alice", [], ACLPermission.WRITE) is True

    def test_grant_group(self):
        """Test granting group permissions."""
        manager = ACLManager()
        acl = ACL.empty()

        manager.grant_group(acl, "developers", read=True, execute=True)
        assert len(acl.entries) == 1
        assert acl.check_permission("alice", ["developers"], ACLPermission.READ) is True
        assert acl.check_permission("alice", ["developers"], ACLPermission.EXECUTE) is True

    def test_revoke_user(self):
        """Test revoking user permissions."""
        manager = ACLManager()
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="alice",
                    permissions={ACLPermission.READ},
                )
            ]
        )

        assert manager.revoke_user(acl, "alice") is True
        assert len(acl.entries) == 0

    def test_revoke_user_not_found(self):
        """Test revoking non-existent user."""
        manager = ACLManager()
        acl = ACL.empty()

        assert manager.revoke_user(acl, "alice") is False

    def test_revoke_group(self):
        """Test revoking group permissions."""
        manager = ACLManager()
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.GROUP,
                    identifier="developers",
                    permissions={ACLPermission.READ},
                )
            ]
        )

        assert manager.revoke_group(acl, "developers") is True
        assert len(acl.entries) == 0

    def test_deny_user(self):
        """Test denying user access."""
        manager = ACLManager()
        acl = ACL.empty()

        manager.deny_user(acl, "bob")
        assert len(acl.entries) == 1
        assert acl.check_permission("bob", [], ACLPermission.READ) is False

    def test_deny_group(self):
        """Test denying group access."""
        manager = ACLManager()
        acl = ACL.empty()

        manager.deny_group(acl, "blocked_group")
        assert len(acl.entries) == 1
        assert acl.check_permission("alice", ["blocked_group"], ACLPermission.READ) is False

    def test_deny_removes_allow(self):
        """Test deny removes existing allow entry."""
        manager = ACLManager()
        acl = ACL(
            entries=[
                ACLEntry(
                    entry_type=ACLEntryType.USER,
                    identifier="bob",
                    permissions={ACLPermission.READ},
                )
            ]
        )

        manager.deny_user(acl, "bob")
        # Should have only one entry (deny)
        assert len(acl.entries) == 1
        assert acl.entries[0].deny
