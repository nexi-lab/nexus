"""Auth brick isolation tests (Decision #11).

Verifies that the auth brick:
- Can be imported without pulling in server/kernel/other brick dependencies
- verify_imports() returns True for all required modules
- Works with a mock session_factory (no real database needed)
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.auth.manifest import verify_imports

# Required modules that must all be importable
_REQUIRED_MODULES = [
    "nexus.auth.types",
    "nexus.auth.protocol",
    "nexus.auth.constants",
    "nexus.auth.cache",
    "nexus.auth.providers.base",
    "nexus.auth.providers.discriminator",
]


def test_verify_imports_all_required():
    """verify_imports() returns True for all required auth modules."""
    result = verify_imports()
    for module in _REQUIRED_MODULES:
        assert result.get(module) is True, f"Required module {module} is not importable"


def test_auth_brick_imports_without_server():
    """Auth brick can be imported without nexus.server.* dependencies.

    This test checks that importing the auth brick's core modules
    does NOT trigger imports from nexus.server or nexus.core.nexus_fs.
    """
    # Save current module state
    modules_before = set(sys.modules.keys())

    # Force fresh imports by removing cached modules
    auth_modules = [k for k in sys.modules if k.startswith("nexus.auth")]
    for mod in auth_modules:
        del sys.modules[mod]

    try:
        # Import the auth brick
        importlib.import_module("nexus.auth.types")
        importlib.import_module("nexus.auth.constants")
        importlib.import_module("nexus.auth.protocol")
        importlib.import_module("nexus.auth.cache")

        # Check that no server modules were pulled in
        new_modules = set(sys.modules.keys()) - modules_before
        server_imports = [m for m in new_modules if m.startswith("nexus.server")]
        nexus_fs_imports = [m for m in new_modules if "nexus_fs" in m or "NexusFS" in m]

        assert not server_imports, f"Auth brick pulled in server modules: {server_imports}"
        assert not nexus_fs_imports, f"Auth brick pulled in NexusFS: {nexus_fs_imports}"
    finally:
        # Restore modules
        for mod in auth_modules:
            if mod not in sys.modules:
                with contextlib.suppress(ImportError):
                    importlib.import_module(mod)


def test_auth_brick_no_rebac_import():
    """Auth brick does not import from nexus.bricks.rebac."""
    modules_before = set(sys.modules.keys())

    auth_modules = [k for k in sys.modules if k.startswith("nexus.auth")]
    for mod in auth_modules:
        del sys.modules[mod]

    try:
        importlib.import_module("nexus.auth.types")
        importlib.import_module("nexus.auth.constants")
        importlib.import_module("nexus.auth.protocol")

        new_modules = set(sys.modules.keys()) - modules_before
        rebac_imports = [m for m in new_modules if m.startswith("nexus.bricks.rebac")]
        assert not rebac_imports, f"Auth brick pulled in rebac modules: {rebac_imports}"
    finally:
        for mod in auth_modules:
            if mod not in sys.modules:
                with contextlib.suppress(ImportError):
                    importlib.import_module(mod)


def test_auth_brick_no_pay_import():
    """Auth brick does not import from nexus.bricks.pay."""
    modules_before = set(sys.modules.keys())

    auth_modules = [k for k in sys.modules if k.startswith("nexus.auth")]
    for mod in auth_modules:
        del sys.modules[mod]

    try:
        importlib.import_module("nexus.auth.types")
        importlib.import_module("nexus.auth.constants")

        new_modules = set(sys.modules.keys()) - modules_before
        pay_imports = [m for m in new_modules if m.startswith("nexus.bricks.pay")]
        assert not pay_imports, f"Auth brick pulled in pay modules: {pay_imports}"
    finally:
        for mod in auth_modules:
            if mod not in sys.modules:
                with contextlib.suppress(ImportError):
                    importlib.import_module(mod)


def test_auth_result_is_frozen():
    """AuthResult is a frozen dataclass (immutable)."""
    from nexus.auth.types import AuthResult

    result = AuthResult(authenticated=True, subject_id="alice")
    with pytest.raises(AttributeError):
        result.subject_id = "bob"  # type: ignore[misc]


def test_auth_result_defaults():
    """AuthResult defaults are sensible."""
    from nexus.auth.types import AuthResult

    result = AuthResult(authenticated=False)
    assert result.subject_type == "user"
    assert result.subject_id is None
    assert result.zone_id is None
    assert result.is_admin is False
    assert result.metadata is None
    assert result.inherit_permissions is True


def test_auth_cache_works_standalone():
    """AuthCache works without any database or server dependencies."""
    from nexus.auth.cache import AuthCache

    cache = AuthCache(ttl=60, max_size=10)
    cache.set("tok", {"user": "alice"})
    assert cache.get("tok") == {"user": "alice"}
    cache.invalidate("tok")
    assert cache.get("tok") is None


def test_auth_constants_available():
    """Auth constants are accessible from the brick."""
    from nexus.auth.constants import (
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


@pytest.mark.asyncio
async def test_auth_service_with_mock_provider():
    """AuthService works with a mock provider (no real DB)."""
    from nexus.auth.service import AuthService
    from nexus.auth.types import AuthResult

    mock_provider = MagicMock()
    mock_provider.authenticate = AsyncMock(
        return_value=AuthResult(authenticated=True, subject_id="mock_user")
    )

    service = AuthService(provider=mock_provider)
    result = await service.authenticate("fake-token")
    assert result.authenticated is True
    assert result.subject_id == "mock_user"
