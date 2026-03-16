"""OAuthConnectorMixin tests (Issue #1601).

Tests for the shared _init_oauth() method that extracts duplicated
TokenManager initialization boilerplate from connector backends.
"""

from unittest.mock import MagicMock, patch

from nexus.backends.connectors.oauth import OAuthConnectorMixin


class _TestOAuthBackend(OAuthConnectorMixin):
    """Concrete class for testing the mixin."""

    pass


class TestInitOAuth:
    """Tests for OAuthConnectorMixin._init_oauth()."""

    @patch("nexus.backends.connectors.utils.resolve_database_url", return_value="/tmp/tokens.db")
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_sets_attributes(
        self, _mock_tm_cls: MagicMock, _mock_resolve: MagicMock
    ) -> None:
        """_init_oauth sets token_manager_db, user_email, and provider."""
        obj = _TestOAuthBackend()
        obj._init_oauth("my_db.sqlite", user_email="a@b.com", provider="gmail")

        assert obj.token_manager_db == "my_db.sqlite"
        assert obj.user_email == "a@b.com"
        assert obj.provider == "gmail"

    @patch("nexus.backends.connectors.utils.resolve_database_url", return_value="/data/tokens.db")
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_db_url_path(self, mock_tm_cls: MagicMock, _mock_resolve: MagicMock) -> None:
        """Plain path (no scheme) creates TokenManager with db_path."""
        obj = _TestOAuthBackend()
        obj._init_oauth("/data/tokens.db")

        mock_tm_cls.assert_called_once_with(db_path="/data/tokens.db")
        assert obj.token_manager is mock_tm_cls.return_value

    @patch(
        "nexus.backends.connectors.utils.resolve_database_url",
        return_value="postgresql://host/db",
    )
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_db_url_postgresql(
        self, mock_tm_cls: MagicMock, _mock_resolve: MagicMock
    ) -> None:
        """PostgreSQL URL creates TokenManager with db_url."""
        obj = _TestOAuthBackend()
        obj._init_oauth("postgresql://host/db")

        mock_tm_cls.assert_called_once_with(db_url="postgresql://host/db")

    @patch(
        "nexus.backends.connectors.utils.resolve_database_url",
        return_value="sqlite:///local.db",
    )
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_db_url_sqlite(
        self, mock_tm_cls: MagicMock, _mock_resolve: MagicMock
    ) -> None:
        """SQLite URL creates TokenManager with db_url."""
        obj = _TestOAuthBackend()
        obj._init_oauth("sqlite:///local.db")

        mock_tm_cls.assert_called_once_with(db_url="sqlite:///local.db")

    @patch("nexus.backends.connectors.utils.resolve_database_url", return_value="/tmp/t.db")
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_default_provider(
        self, _mock_tm_cls: MagicMock, _mock_resolve: MagicMock
    ) -> None:
        """Provider defaults to 'oauth' when not specified."""
        obj = _TestOAuthBackend()
        obj._init_oauth("db.sqlite")

        assert obj.provider == "oauth"

    @patch("nexus.backends.connectors.utils.resolve_database_url", return_value="/tmp/t.db")
    @patch("nexus.bricks.auth.oauth.token_manager.TokenManager")
    def test_init_oauth_with_user_email(
        self, _mock_tm_cls: MagicMock, _mock_resolve: MagicMock
    ) -> None:
        """user_email is stored and defaults to None."""
        obj_with = _TestOAuthBackend()
        obj_with._init_oauth("db.sqlite", user_email="user@example.com")
        assert obj_with.user_email == "user@example.com"

        obj_without = _TestOAuthBackend()
        obj_without._init_oauth("db.sqlite")
        assert obj_without.user_email is None
