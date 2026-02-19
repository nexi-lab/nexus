"""Tests for RPC method name validation security (Issue #2136).

Ensures that:
- Private/malformed method names are blocked at dispatch time
- Discovery filter rejects rpc_name bypass attempts
- Valid method names still dispatch correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.server.rpc.dispatch import dispatch_method

# =============================================================================
# dispatch_method() — runtime method name validation
# =============================================================================


class TestRPCMethodNameValidation:
    """Verify dispatch_method() rejects private/malformed method names."""

    @pytest.fixture
    def mock_nexus_fs(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def exposed_methods(self) -> dict:
        return {"read": MagicMock(), "write": MagicMock()}

    @pytest.mark.asyncio
    async def test_underscore_method_rejected(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Methods starting with '_' must be blocked."""
        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "_private_method",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )

    @pytest.mark.asyncio
    async def test_dot_method_rejected(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Methods containing '.' must be blocked."""
        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "os.system",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )

    @pytest.mark.asyncio
    async def test_empty_method_rejected(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Empty method name must be blocked."""
        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )

    @pytest.mark.asyncio
    async def test_dunder_method_rejected(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Dunder methods must be blocked."""
        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "__init__",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )

    @pytest.mark.asyncio
    async def test_unknown_valid_method_raises_not_found(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Unknown but validly-named methods raise generic 'Method not found'."""
        with pytest.raises(ValueError, match="Method not found"):
            await dispatch_method(
                "nonexistent_method",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )

    @pytest.mark.asyncio
    async def test_error_message_does_not_echo_method_name(
        self, mock_nexus_fs: MagicMock, exposed_methods: dict
    ) -> None:
        """Error messages must not echo the method name (security)."""
        with pytest.raises(ValueError) as exc_info:
            await dispatch_method(
                "_secret_internal",
                params=MagicMock(),
                context=MagicMock(),
                nexus_fs=mock_nexus_fs,
                exposed_methods=exposed_methods,
            )
        assert "_secret_internal" not in str(exc_info.value)


# =============================================================================
# _discover_exposed_methods() — rpc_name bypass prevention
# =============================================================================


class TestDiscoveryFilterRpcName:
    """Verify _discover_exposed_methods() filters out private rpc_name aliases."""

    def test_rpc_name_underscore_filtered(self) -> None:
        """Methods with rpc_name starting with '_' must be excluded from discovery."""
        from nexus.server.fastapi_server import _discover_exposed_methods

        def private_method():
            pass

        private_method._rpc_exposed = True  # type: ignore[attr-defined]
        private_method._rpc_name = "_hidden"  # type: ignore[attr-defined]

        def normal_method():
            pass

        normal_method._rpc_exposed = True  # type: ignore[attr-defined]
        normal_method._rpc_name = "visible"  # type: ignore[attr-defined]

        class FakeFS:
            good_method = staticmethod(normal_method)
            bad_method = staticmethod(private_method)

        exposed = _discover_exposed_methods(FakeFS())  # type: ignore[arg-type]
        assert "visible" in exposed
        assert "_hidden" not in exposed

    def test_rpc_name_normal_passes(self) -> None:
        """Methods with normal rpc_name are included in discovery."""
        from nexus.server.fastapi_server import _discover_exposed_methods

        def method():
            pass

        method._rpc_exposed = True  # type: ignore[attr-defined]
        method._rpc_name = "safe_method"  # type: ignore[attr-defined]

        class FakeFS:
            my_method = staticmethod(method)

        exposed = _discover_exposed_methods(FakeFS())  # type: ignore[arg-type]
        assert "safe_method" in exposed
