"""E2E tests for RPC server with authentication.

Tests the _run_async_safe optimization with real auth_provider calls.
"""

from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import Base


class TestRPCServerAuthQuickCheck:
    """Quick auth validation tests without full server startup."""

    def test_run_async_safe_with_real_auth_provider(self, tmp_path):
        """Test _run_async_safe with a real DatabaseAPIKeyAuth provider."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.server.rpc_server import RPCRequestHandler

        # Reset executor state
        RPCRequestHandler._async_executor = None
        RPCRequestHandler._async_executor_lock = None

        # Setup database
        db_path = tmp_path / "auth.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        # Create key
        with session_factory() as session:
            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="user1",
                name="Test",
            )
            session.commit()

        # Create auth provider
        auth_provider = DatabaseAPIKeyAuth(session_factory)

        # Create mock handler with real auth provider
        import asyncio
        from unittest.mock import Mock

        handler = Mock(spec=RPCRequestHandler)
        handler.auth_provider = auth_provider
        handler.api_key = None
        handler.event_loop = asyncio.new_event_loop()
        handler.headers = {"Authorization": f"Bearer {raw_key}"}

        # Bind actual methods
        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)
        handler._validate_auth = lambda: RPCRequestHandler._validate_auth(handler)

        try:
            # Test authentication - this exercises _run_async_safe
            result = handler._validate_auth()
            assert result is True, "Authentication should succeed"

            # Verify executor was created
            assert RPCRequestHandler._async_executor is not None

            # Test multiple calls reuse executor
            executor1 = RPCRequestHandler._async_executor
            result2 = handler._validate_auth()
            assert result2 is True
            assert RPCRequestHandler._async_executor is executor1
        finally:
            handler.event_loop.close()
            if RPCRequestHandler._async_executor is not None:
                RPCRequestHandler._async_executor.shutdown(wait=True)
                RPCRequestHandler._async_executor = None

    def test_run_async_safe_with_invalid_token(self, tmp_path):
        """Test _run_async_safe with invalid token."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.server.rpc_server import RPCRequestHandler

        # Reset
        RPCRequestHandler._async_executor = None
        RPCRequestHandler._async_executor_lock = None

        # Setup
        db_path = tmp_path / "auth.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        auth_provider = DatabaseAPIKeyAuth(session_factory)

        import asyncio
        from unittest.mock import Mock

        handler = Mock(spec=RPCRequestHandler)
        handler.auth_provider = auth_provider
        handler.api_key = None
        handler.event_loop = asyncio.new_event_loop()
        handler.headers = {"Authorization": "Bearer invalid-token"}

        handler._run_async_safe = lambda coro: RPCRequestHandler._run_async_safe(handler, coro)
        handler._validate_auth = lambda: RPCRequestHandler._validate_auth(handler)

        try:
            result = handler._validate_auth()
            assert result is False, "Invalid token should fail auth"
        finally:
            handler.event_loop.close()
            if RPCRequestHandler._async_executor is not None:
                RPCRequestHandler._async_executor.shutdown(wait=True)
                RPCRequestHandler._async_executor = None
