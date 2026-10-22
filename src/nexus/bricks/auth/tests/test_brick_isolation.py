"""Auth brick isolation tests (Decision #11).

Verifies that the auth brick:
- Can be imported without pulling in server/kernel/other brick dependencies
- verify_imports() returns True for all required modules
- Works with a mock session_factory (no real database needed)
"""

import os
import subprocess
import sys

import pytest

from nexus.bricks.auth.manifest import verify_imports

# Required modules that must all be importable
_REQUIRED_MODULES = [
    "nexus.bricks.auth.types",
    "nexus.bricks.auth.protocol",
    "nexus.bricks.auth.constants",
    "nexus.bricks.auth.cache",
    "nexus.bricks.auth.providers.base",
    "nexus.bricks.auth.providers.discriminator",
]


def test_verify_imports_all_required() -> None:
    """verify_imports() returns True for all required auth modules."""
    result = verify_imports()
    for module in _REQUIRED_MODULES:
        assert result.get(module) is True, f"Required module {module} is not importable"


def _assert_isolated_imports(forbidden: tuple[str, ...]) -> None:
    script = f"""
import importlib
import sys
for module in {_REQUIRED_MODULES!r}:
    importlib.import_module(module)
forbidden = {forbidden!r}
leaked = [name for name in sys.modules if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden)]
assert not leaked, leaked
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_auth_brick_imports_without_server() -> None:
    _assert_isolated_imports(("nexus.server", "nexus.core.nexus_fs", "nexus_fs"))


def test_auth_brick_no_rebac_import() -> None:
    _assert_isolated_imports(("nexus.bricks.rebac",))


def test_auth_brick_no_pay_import() -> None:
    _assert_isolated_imports(("nexus.bricks.pay",))


def test_auth_result_is_frozen() -> None:
    """AuthResult is a frozen dataclass (immutable)."""
    from nexus.bricks.auth.types import AuthResult

    result = AuthResult(authenticated=True, subject_id="alice")
    with pytest.raises(AttributeError):
        result.subject_id = "bob"  # type: ignore[misc]


def test_auth_result_defaults() -> None:
    """AuthResult defaults are sensible."""
    from nexus.bricks.auth.types import AuthResult

    result = AuthResult(authenticated=False)
    assert result.subject_type == "user"
    assert result.subject_id is None
    assert result.zone_id is None
    assert result.is_admin is False
    assert result.metadata is None
    assert result.inherit_permissions is True


def test_auth_cache_works_standalone() -> None:
    """AuthCache works without any database or server dependencies."""
    from nexus.bricks.auth.cache import AuthCache

    cache = AuthCache(ttl=60, max_size=10)
    cache.set("tok", {"user": "alice"})
    assert cache.get("tok") == {"user": "alice"}
    cache.invalidate("tok")
    assert cache.get("tok") is None


def test_auth_constants_available() -> None:
    """Auth constants are accessible from the brick."""
    from nexus.bricks.auth.constants import (
        API_KEY_MIN_LENGTH,
        API_KEY_PREFIX,
        HMAC_SALT,
        PERSONAL_EMAIL_DOMAINS,
        RESERVED_ZONE_IDS,
    )

    assert API_KEY_PREFIX == "sk-"
    assert API_KEY_MIN_LENGTH > 0
    assert len(HMAC_SALT) > 0
    assert isinstance(PERSONAL_EMAIL_DOMAINS, frozenset)
    assert isinstance(RESERVED_ZONE_IDS, frozenset)
    assert "gmail.com" in PERSONAL_EMAIL_DOMAINS
    assert "admin" in RESERVED_ZONE_IDS
