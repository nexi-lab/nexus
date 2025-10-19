"""Unit tests for UNIX-style permissions."""

import pytest

from nexus.core.permissions import (
    FileMode,
    FilePermissions,
    Permission,
    PermissionChecker,
    parse_mode,
)


class TestPermission:
    """Test Permission flags."""

    def test_permission_values(self):
        """Test permission bit values."""
        assert Permission.NONE == 0
        assert Permission.EXECUTE == 1
        assert Permission.WRITE == 2
        assert Permission.READ == 4
        assert Permission.ALL == 7

    def test_permission_combinations(self):
        """Test permission combinations."""
        read_write = Permission.READ | Permission.WRITE
        assert read_write == 6

        read_execute = Permission.READ | Permission.EXECUTE
        assert read_execute == 5

        all_perms = Permission.READ | Permission.WRITE | Permission.EXECUTE
        assert all_perms == 7


class TestFileMode:
    """Test FileMode class."""

    def test_init_default(self):
        """Test default mode initialization."""
        mode = FileMode()
        assert mode.mode == 0o644

    def test_init_custom(self):
        """Test custom mode initialization."""
        mode = FileMode(0o755)
        assert mode.mode == 0o755

    def test_init_invalid(self):
        """Test invalid mode raises error."""
        with pytest.raises(ValueError, match="mode must be between"):
            FileMode(0o1000)  # Too large

        with pytest.raises(ValueError, match="mode must be between"):
            FileMode(-1)  # Negative

    def test_owner_permissions(self):
        """Test owner permission checks."""
        mode = FileMode(0o755)  # rwxr-xr-x
        assert mode.owner_can_read()
        assert mode.owner_can_write()
        assert mode.owner_can_execute()

    def test_group_permissions(self):
        """Test group permission checks."""
        mode = FileMode(0o755)  # rwxr-xr-x
        assert mode.group_can_read()
        assert not mode.group_can_write()
        assert mode.group_can_execute()

    def test_other_permissions(self):
        """Test other permission checks."""
        mode = FileMode(0o755)  # rwxr-xr-x
        assert mode.other_can_read()
        assert not mode.other_can_write()
        assert mode.other_can_execute()

    def test_mode_644(self):
        """Test mode 644 (rw-r--r--)."""
        mode = FileMode(0o644)
        # Owner
        assert mode.owner_can_read()
        assert mode.owner_can_write()
        assert not mode.owner_can_execute()
        # Group
        assert mode.group_can_read()
        assert not mode.group_can_write()
        assert not mode.group_can_execute()
        # Other
        assert mode.other_can_read()
        assert not mode.other_can_write()
        assert not mode.other_can_execute()

    def test_mode_700(self):
        """Test mode 700 (rwx------)."""
        mode = FileMode(0o700)
        # Owner
        assert mode.owner_can_read()
        assert mode.owner_can_write()
        assert mode.owner_can_execute()
        # Group
        assert not mode.group_can_read()
        assert not mode.group_can_write()
        assert not mode.group_can_execute()
        # Other
        assert not mode.other_can_read()
        assert not mode.other_can_write()
        assert not mode.other_can_execute()

    def test_to_string(self):
        """Test converting mode to string."""
        assert FileMode(0o755).to_string() == "rwxr-xr-x"
        assert FileMode(0o644).to_string() == "rw-r--r--"
        assert FileMode(0o700).to_string() == "rwx------"
        assert FileMode(0o000).to_string() == "---------"
        assert FileMode(0o777).to_string() == "rwxrwxrwx"

    def test_from_string(self):
        """Test parsing mode from string."""
        mode = FileMode.from_string("rwxr-xr-x")
        assert mode.mode == 0o755

        mode = FileMode.from_string("rw-r--r--")
        assert mode.mode == 0o644

        mode = FileMode.from_string("rwx------")
        assert mode.mode == 0o700

    def test_from_string_invalid(self):
        """Test invalid string raises error."""
        with pytest.raises(ValueError, match="must be 9 characters"):
            FileMode.from_string("rwx")

        with pytest.raises(ValueError, match="must be 9 characters"):
            FileMode.from_string("rwxrwxrwxrwx")

    def test_repr(self):
        """Test mode repr."""
        mode = FileMode(0o755)
        assert repr(mode) == "FileMode(0o755)"

    def test_str(self):
        """Test mode str."""
        mode = FileMode(0o755)
        assert str(mode) == "rwxr-xr-x"

    def test_equality(self):
        """Test mode equality."""
        mode1 = FileMode(0o755)
        mode2 = FileMode(0o755)
        mode3 = FileMode(0o644)

        assert mode1 == mode2
        assert mode1 != mode3


class TestFilePermissions:
    """Test FilePermissions class."""

    def test_init(self):
        """Test permissions initialization."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert perms.owner == "alice"
        assert perms.group == "developers"
        assert perms.mode.mode == 0o755

    def test_init_missing_owner(self):
        """Test missing owner raises error."""
        with pytest.raises(ValueError, match="owner is required"):
            FilePermissions(owner="", group="developers", mode=FileMode(0o755))

    def test_init_missing_group(self):
        """Test missing group raises error."""
        with pytest.raises(ValueError, match="group is required"):
            FilePermissions(owner="alice", group="", mode=FileMode(0o755))

    def test_init_invalid_mode(self):
        """Test invalid mode raises error."""
        with pytest.raises(TypeError, match="mode must be FileMode"):
            FilePermissions(owner="alice", group="developers", mode=0o755)  # type: ignore

    def test_owner_can_read(self):
        """Test owner can read."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert perms.can_read("alice", [])

    def test_owner_can_write(self):
        """Test owner can write."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert perms.can_write("alice", [])

    def test_group_can_read(self):
        """Test group member can read."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert perms.can_read("bob", ["developers"])

    def test_group_cannot_write(self):
        """Test group member cannot write."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert not perms.can_write("bob", ["developers"])

    def test_other_can_read(self):
        """Test other can read."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert perms.can_read("charlie", ["admins"])

    def test_other_cannot_write(self):
        """Test other cannot write."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o755))
        assert not perms.can_write("charlie", ["admins"])

    def test_mode_600(self):
        """Test mode 600 (rw-------)."""
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o600))
        # Owner
        assert perms.can_read("alice", [])
        assert perms.can_write("alice", [])
        # Group
        assert not perms.can_read("bob", ["developers"])
        assert not perms.can_write("bob", ["developers"])
        # Other
        assert not perms.can_read("charlie", ["admins"])
        assert not perms.can_write("charlie", ["admins"])

    def test_default(self):
        """Test default permissions."""
        perms = FilePermissions.default("alice", "developers")
        assert perms.owner == "alice"
        assert perms.group == "developers"
        assert perms.mode.mode == 0o644

    def test_default_no_group(self):
        """Test default permissions without group."""
        perms = FilePermissions.default("alice")
        assert perms.owner == "alice"
        assert perms.group == "alice"  # Defaults to owner
        assert perms.mode.mode == 0o644

    def test_default_directory(self):
        """Test default directory permissions."""
        perms = FilePermissions.default_directory("alice", "developers")
        assert perms.owner == "alice"
        assert perms.group == "developers"
        assert perms.mode.mode == 0o755


class TestPermissionChecker:
    """Test PermissionChecker class."""

    def test_init_defaults(self):
        """Test default initialization."""
        checker = PermissionChecker()
        assert checker.default_owner == "root"
        assert checker.default_group == "root"

    def test_init_custom(self):
        """Test custom initialization."""
        checker = PermissionChecker(default_owner="alice", default_group="developers")
        assert checker.default_owner == "alice"
        assert checker.default_group == "developers"

    def test_check_read_allowed(self):
        """Test check read allowed."""
        checker = PermissionChecker()
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o644))
        assert checker.check_read(perms, "alice", [])
        assert checker.check_read(perms, "bob", ["developers"])
        assert checker.check_read(perms, "charlie", ["admins"])

    def test_check_write_allowed(self):
        """Test check write allowed."""
        checker = PermissionChecker()
        perms = FilePermissions(owner="alice", group="developers", mode=FileMode(0o644))
        assert checker.check_write(perms, "alice", [])
        assert not checker.check_write(perms, "bob", ["developers"])
        assert not checker.check_write(perms, "charlie", ["admins"])

    def test_check_no_permissions(self):
        """Test check with no permissions set."""
        checker = PermissionChecker()
        # No permissions = allow all (for backward compatibility)
        assert checker.check_read(None, "anyone", [])
        assert checker.check_write(None, "anyone", [])
        assert checker.check_execute(None, "anyone", [])

    def test_create_default_permissions_file(self):
        """Test create default file permissions."""
        checker = PermissionChecker(default_owner="alice", default_group="developers")
        perms = checker.create_default_permissions()
        assert perms.owner == "alice"
        assert perms.group == "developers"
        assert perms.mode.mode == 0o644

    def test_create_default_permissions_directory(self):
        """Test create default directory permissions."""
        checker = PermissionChecker(default_owner="alice", default_group="developers")
        perms = checker.create_default_permissions(is_directory=True)
        assert perms.owner == "alice"
        assert perms.group == "developers"
        assert perms.mode.mode == 0o755

    def test_create_permissions_with_override(self):
        """Test create permissions with custom owner/group."""
        checker = PermissionChecker(default_owner="root", default_group="root")
        perms = checker.create_default_permissions(owner="bob", group="admins")
        assert perms.owner == "bob"
        assert perms.group == "admins"


class TestParseMode:
    """Test parse_mode function."""

    def test_parse_octal(self):
        """Test parsing octal modes."""
        assert parse_mode("755") == 0o755
        assert parse_mode("644") == 0o644
        assert parse_mode("700") == 0o700
        assert parse_mode("777") == 0o777
        assert parse_mode("000") == 0o000

    def test_parse_octal_with_prefix(self):
        """Test parsing octal with 0o prefix."""
        assert parse_mode("0o755") == 0o755
        assert parse_mode("0o644") == 0o644
        assert parse_mode("0O755") == 0o755  # Capital O

    def test_parse_octal_with_zero_prefix(self):
        """Test parsing octal with 0 prefix."""
        assert parse_mode("0755") == 0o755
        assert parse_mode("0644") == 0o644

    def test_parse_symbolic(self):
        """Test parsing symbolic modes."""
        assert parse_mode("rwxr-xr-x") == 0o755
        assert parse_mode("rw-r--r--") == 0o644
        assert parse_mode("rwx------") == 0o700
        assert parse_mode("---------") == 0o000
        assert parse_mode("rwxrwxrwx") == 0o777

    def test_parse_with_whitespace(self):
        """Test parsing with whitespace."""
        assert parse_mode("  755  ") == 0o755
        assert parse_mode("  rwxr-xr-x  ") == 0o755

    def test_parse_invalid_octal(self):
        """Test parsing invalid octal."""
        with pytest.raises(ValueError, match="invalid mode string"):
            parse_mode("888")  # 8 not valid in octal

        with pytest.raises(ValueError, match="invalid mode string"):
            parse_mode("abc")

    def test_parse_invalid_symbolic(self):
        """Test parsing invalid symbolic."""
        with pytest.raises(ValueError, match="invalid mode string"):
            parse_mode("rwx")  # Too short

        with pytest.raises(ValueError, match="invalid mode string"):
            parse_mode("rwxrwxrwxrwx")  # Too long

    def test_parse_out_of_range(self):
        """Test parsing out of range mode."""
        with pytest.raises(ValueError, match="invalid mode string"):
            parse_mode("1000")  # Too large
