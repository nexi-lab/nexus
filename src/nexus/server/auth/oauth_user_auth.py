"""OAuth authentication provider for user login.

This module handles OAuth-based user authentication (logging users into Nexus)
as distinct from backend OAuth integrations (accessing Google Drive, etc.).

Key Distinction:
- This module (UserOAuthAccountModel): User logs in with Google → gets Nexus access
- Existing OAuth (OAuthCredentialModel): User connects Google Drive → accesses their files

Supports:
- Google OAuth for user authentication
- User account creation from OAuth
- OAuth account linking to existing users
- Email verification checks before linking
- Race condition protection
"""

import json
import logging
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nexus.core.permissions import OperationContext
from nexus.server.auth.google_oauth import GoogleOAuthProvider
from nexus.server.auth.local import LocalAuth
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.server.auth.user_helpers import get_user_by_email
from nexus.storage.models import UserModel, UserOAuthAccountModel

logger = logging.getLogger(__name__)


class OAuthUserAuth:
    """OAuth authentication provider for user login.

    Handles OAuth-based user authentication (login) separate from backend integrations.
    Supports Google OAuth with automatic user creation and account linking.

    Features:
    - OAuth token exchange
    - User account creation from OAuth
    - Email-based account linking (with verification checks)
    - Race condition protection
    - JWT token generation for Nexus access

    Example usage:
        # Initialize provider
        oauth_auth = OAuthUserAuth(
            session_factory=session_factory,
            google_client_id="xxx.apps.googleusercontent.com",
            google_client_secret="GOCSPX-xxx",
            google_redirect_uri="http://localhost:2026/auth/oauth/callback",
            jwt_secret="your-jwt-secret",
            oauth_crypto=oauth_crypto
        )

        # Get authorization URL
        auth_url, state = oauth_auth.get_google_auth_url()

        # Handle callback
        user, token = await oauth_auth.handle_google_callback(code, state)
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        google_client_id: str,
        google_client_secret: str,
        google_redirect_uri: str,
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
        oauth_crypto: OAuthCrypto | None = None,
    ):
        """Initialize OAuth user authentication provider.

        Args:
            session_factory: SQLAlchemy session factory
            google_client_id: Google OAuth client ID
            google_client_secret: Google OAuth client secret
            google_redirect_uri: OAuth redirect URI (must match Google Console)
            jwt_secret: JWT secret for token signing
            token_expiry: JWT token expiration in seconds (default: 3600)
            oauth_crypto: OAuth encryption service (created if not provided)
        """
        self.session_factory = session_factory
        self.jwt_secret = jwt_secret or secrets.token_urlsafe(32)
        self.token_expiry = token_expiry
        self.oauth_crypto = oauth_crypto or OAuthCrypto()

        # Initialize Google OAuth provider for user authentication
        # Using openid, email, and profile scopes for authentication
        self.google_provider = GoogleOAuthProvider(
            client_id=google_client_id,
            client_secret=google_client_secret,
            redirect_uri=google_redirect_uri,
            scopes=[
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
            provider_name="google-auth",
        )

        # Initialize LocalAuth for JWT token creation
        self.local_auth = LocalAuth(jwt_secret=self.jwt_secret, token_expiry=token_expiry)

        logger.info("Initialized OAuthUserAuth with Google OAuth")

    def get_google_auth_url(self, redirect_uri: str | None = None) -> tuple[str, str]:
        """Get Google OAuth authorization URL.

        Args:
            redirect_uri: Optional redirect URI to use after OAuth callback.
                         If not provided, uses default from OAuth provider config.

        Returns:
            Tuple of (authorization_url, state)
            State should be stored in session for CSRF protection
        """
        state = secrets.token_urlsafe(32)
        auth_url = self.google_provider.get_authorization_url(
            state=state, redirect_uri=redirect_uri
        )
        return auth_url, state

    async def handle_google_callback(
        self, code: str, _state: str | None = None, redirect_uri: str | None = None
    ) -> tuple[UserModel, str]:
        """Handle Google OAuth callback.

        Exchanges authorization code for tokens, creates/links user account,
        and returns JWT token for Nexus access.

        Args:
            code: Authorization code from OAuth callback
            state: State parameter for CSRF protection (optional, should be validated by caller)
            redirect_uri: Optional redirect URI to use for token exchange.
                         Must match the redirect_uri used in authorization URL.
                         If not provided, uses default from OAuth provider config.

        Returns:
            Tuple of (UserModel, JWT token)

        Raises:
            ValueError: If OAuth flow fails or email verification issues
        """
        # Exchange code for tokens
        try:
            oauth_credential = await self.google_provider.exchange_code(
                code, redirect_uri=redirect_uri
            )
        except Exception as e:
            logger.error(f"Failed to exchange OAuth code: {e}")
            raise ValueError(f"OAuth token exchange failed: {e}") from e

        # Extract user info from ID token
        # Google returns an ID token (JWT) with user claims
        user_info = await self._extract_google_user_info(oauth_credential.access_token)

        provider_user_id = user_info.get("sub")  # Google user ID
        provider_email = user_info.get("email")
        email_verified = user_info.get("email_verified", False)
        name = user_info.get("name")
        picture = user_info.get("picture")

        if not provider_user_id:
            raise ValueError("OAuth response missing 'sub' claim (user ID)")

        # Create or link user account with race condition protection
        with self.session_factory() as session:
            user, is_new = await self._get_or_create_oauth_user(
                session=session,
                provider="google",
                provider_user_id=provider_user_id,
                provider_email=provider_email,
                email_verified=email_verified,
                name=name,
                picture=picture,
                oauth_credential=oauth_credential,
            )

            # Generate JWT token for Nexus access
            user_info_dict = {
                "subject_type": "user",
                "subject_id": user.user_id,
                "tenant_id": None,  # TODO: Get from ReBAC groups
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
            }

            # Email must exist for token creation
            email_for_token = user.email or provider_email
            assert email_for_token is not None, "Email is required for token creation"
            token = self.local_auth.create_token(email_for_token, user_info_dict)

            if is_new:
                logger.info(
                    f"Created new user from Google OAuth: {provider_email} (user_id={user.user_id})"
                )
            else:
                logger.info(
                    f"Logged in existing user via Google OAuth: {provider_email} (user_id={user.user_id})"
                )

            return user, token

    async def _extract_google_user_info(self, access_token: str) -> dict[str, Any]:
        """Extract user info from Google OAuth access token.

        Args:
            access_token: Google OAuth access token

        Returns:
            Dictionary with user claims (sub, email, name, picture, etc.)

        Raises:
            ValueError: If userinfo request fails
        """
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except Exception as e:
            logger.error(f"Failed to fetch Google userinfo: {e}")
            raise ValueError(f"Failed to fetch user info: {e}") from e

    async def _get_or_create_oauth_user(
        self,
        session: Session,
        provider: str,
        provider_user_id: str,
        provider_email: str | None,
        email_verified: bool,
        name: str | None,
        picture: str | None,
        oauth_credential: Any,
    ) -> tuple[UserModel, bool]:
        """Get existing user or create new user from OAuth.

        Handles race conditions when multiple requests try to create the same user.
        Implements email-based account linking with verification checks.

        Args:
            session: Database session
            provider: OAuth provider name (e.g., "google")
            provider_user_id: User ID from OAuth provider
            provider_email: Email from OAuth provider
            email_verified: Whether email is verified by OAuth provider
            name: Display name from OAuth provider
            picture: Avatar URL from OAuth provider
            oauth_credential: OAuth credential object

        Returns:
            Tuple of (UserModel, is_new_user)

        Security:
            - ONLY auto-links if both emails are verified
            - Race condition protection via unique constraint on (provider, provider_user_id)
        """
        with session.begin():
            # CRITICAL: Lock on provider_user_id to prevent concurrent creation
            # Try to get existing OAuth account
            stmt = select(UserOAuthAccountModel).where(
                UserOAuthAccountModel.provider == provider,
                UserOAuthAccountModel.provider_user_id == provider_user_id,
            )

            existing_oauth = session.scalar(stmt)

            if existing_oauth:
                # OAuth account exists - return existing user
                user = session.get(UserModel, existing_oauth.user_id)
                if not user or user.is_active == 0:
                    raise ValueError("User account is inactive")

                # Update last_used_at
                existing_oauth.last_used_at = datetime.utcnow()
                session.add(existing_oauth)
                session.flush()

                # Make instance detached so it can be accessed after session closes
                session.expunge(user)

                return user, False

            # OAuth account doesn't exist - check for existing user by email
            existing_user = None
            if provider_email and email_verified:
                existing_user = get_user_by_email(session, provider_email)

            if existing_user and existing_user.email_verified == 1:
                # SECURITY: Both emails verified - safe to link OAuth account to existing user
                try:
                    await self._create_oauth_account(
                        session=session,
                        user_id=existing_user.user_id,
                        provider=provider,
                        provider_user_id=provider_user_id,
                        provider_email=provider_email,
                        picture=picture,
                        oauth_credential=oauth_credential,
                    )
                    session.flush()

                    # Make instance detached so it can be accessed after session closes
                    session.expunge(existing_user)

                    logger.info(f"Linked OAuth account to existing user: {provider_email}")
                    return existing_user, False
                except IntegrityError:
                    # Race condition: Another request created the OAuth account
                    session.rollback()
                    # Retry lookup
                    existing_oauth = session.scalar(stmt)
                    if existing_oauth:
                        user = session.get(UserModel, existing_oauth.user_id)
                        if not user:
                            raise ValueError("User not found for OAuth account") from None
                        session.expunge(user)
                        return user, False
                    raise  # Unexpected error

            elif existing_user and existing_user.email_verified == 0:
                # SECURITY: Existing user email not verified - don't auto-link
                logger.warning(
                    f"OAuth email matches existing user but email not verified: {provider_email}"
                )
                # Create new user account (user will need to verify and merge manually)

            # Create new user
            user_id = str(uuid.uuid4())
            user = UserModel(
                user_id=user_id,
                email=provider_email,
                username=None,  # OAuth users don't have username by default
                display_name=name or provider_email.split("@")[0]
                if provider_email
                else "OAuth User",
                avatar_url=picture,
                password_hash=None,  # OAuth users don't have password
                primary_auth_method="oauth",
                is_global_admin=0,
                is_active=1,
                email_verified=1 if email_verified else 0,
                user_metadata=None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )

            session.add(user)
            session.flush()

            # Create OAuth account with race condition protection
            try:
                await self._create_oauth_account(
                    session=session,
                    user_id=user_id,
                    provider=provider,
                    provider_user_id=provider_user_id,
                    provider_email=provider_email,
                    picture=picture,
                    oauth_credential=oauth_credential,
                )
                session.flush()

                # Make instance detached so it can be accessed after session closes
                session.expunge(user)

                logger.info(f"Created new user from OAuth: {provider_email} (user_id={user_id})")

                # Provision user resources (tenant, directories, workspace, agents, skills, API key)
                if provider_email:
                    await self._provision_oauth_user(
                        user_id=user_id,
                        email=provider_email,
                        display_name=user.display_name,
                    )
                else:
                    logger.warning(
                        f"Cannot provision OAuth user {user_id}: no email provided by OAuth provider"
                    )

                return user, True

            except IntegrityError:
                # Race condition: Another request created the user/OAuth account
                session.rollback()
                # Retry lookup
                existing_oauth = session.scalar(stmt)
                if existing_oauth:
                    user = session.get(UserModel, existing_oauth.user_id)
                    if not user:
                        raise ValueError("User not found for OAuth account") from None
                    session.expunge(user)
                    logger.info(f"Race condition resolved: Using existing user {user.user_id}")
                    return user, False
                raise  # Unexpected error

    async def _create_oauth_account(
        self,
        session: Session,
        user_id: str,
        provider: str,
        provider_user_id: str,
        provider_email: str | None,
        picture: str | None,
        oauth_credential: Any,
    ) -> UserOAuthAccountModel:
        """Create OAuth account record.

        Args:
            session: Database session
            user_id: User ID to link to
            provider: OAuth provider name
            provider_user_id: User ID from OAuth provider
            provider_email: Email from OAuth provider
            picture: Avatar URL
            oauth_credential: OAuth credential object

        Returns:
            Created UserOAuthAccountModel

        Raises:
            IntegrityError: If OAuth account already exists (race condition)
        """
        # Encrypt ID token (if available)
        # For authentication, we primarily use ID tokens, not access/refresh tokens
        encrypted_id_token = None
        if hasattr(oauth_credential, "id_token") and oauth_credential.id_token:
            encrypted_id_token = self.oauth_crypto.encrypt_token(oauth_credential.id_token)

        # Store provider profile data
        provider_profile = json.dumps(
            {
                "name": oauth_credential.metadata.get("name")
                if oauth_credential.metadata
                else None,
                "picture": picture,
                "email": provider_email,
            }
        )

        oauth_account = UserOAuthAccountModel(
            oauth_account_id=str(uuid.uuid4()),
            user_id=user_id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=provider_email,
            encrypted_id_token=encrypted_id_token,
            token_expires_at=(
                oauth_credential.expires_at
                if oauth_credential and oauth_credential.expires_at
                else None
            ),
            provider_profile=provider_profile,
            created_at=datetime.utcnow(),
            last_used_at=datetime.utcnow(),
        )

        session.add(oauth_account)
        return oauth_account

    async def _provision_oauth_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None,
    ) -> None:
        """Provision user resources after OAuth user creation.

        Creates all necessary resources for a new OAuth user:
        - TenantModel (if tenant doesn't exist)
        - User directories (workspace, memory, skill, agent, connector, resource)
        - Default workspace
        - Default agents (ImpersonatedUser, UntrustedAgent)
        - Default skills (all from data/skills/)
        - API key for programmatic access
        - ReBAC permissions (user as tenant owner)
        - Entity registry entries

        Args:
            user_id: User ID (UUID)
            email: User email (used to extract tenant_id)
            display_name: User display name

        Notes:
            - Errors are logged but don't fail OAuth login
            - Idempotent: safe to call multiple times
            - Requires NexusFS instance to be set via set_nexus_instance()
        """
        # Import here to avoid circular dependency
        from nexus.server.auth.auth_routes import get_nexus_instance

        nx = get_nexus_instance()
        if nx is None:
            logger.error(
                "Cannot provision OAuth user: NexusFS instance not available. "
                "User created but missing tenant, directories, workspace, agents, skills, API key."
            )
            return

        # Extract tenant_id from email (e.g., alice@gmail.com → tenant_id "alice")
        tenant_id = email.split("@")[0] if email else user_id

        # Create admin context for provisioning
        admin_context = OperationContext(
            user="system",
            groups=[],
            tenant_id=tenant_id,
            is_admin=True,
        )

        try:
            logger.info(
                f"Provisioning OAuth user resources: user_id={user_id}, tenant_id={tenant_id}"
            )

            result = nx.provision_user(
                user_id=user_id,
                email=email,
                display_name=display_name,
                tenant_id=tenant_id,
                create_api_key=True,  # OAuth users need API keys for programmatic access
                create_agents=True,
                import_skills=True,
                context=admin_context,
            )

            logger.info(
                f"Successfully provisioned OAuth user: "
                f"user_id={user_id}, "
                f"tenant_id={result['tenant_id']}, "
                f"workspace={result['workspace_path']}, "
                f"agents={len(result['agent_paths'])}, "
                f"skills={len(result['skill_paths'])}"
            )

        except Exception as e:
            # Log error but don't fail OAuth login - user can be provisioned later
            logger.error(
                f"Failed to provision OAuth user resources (user_id={user_id}): {e}",
                exc_info=True,
            )

    def get_user_oauth_accounts(self, user_id: str) -> list[dict[str, Any]]:
        """Get list of OAuth accounts linked to user.

        Args:
            user_id: User ID

        Returns:
            List of OAuth account info dicts
        """
        with self.session_factory() as session:
            stmt = select(UserOAuthAccountModel).where(UserOAuthAccountModel.user_id == user_id)
            accounts = session.scalars(stmt).all()

            return [
                {
                    "oauth_account_id": account.oauth_account_id,
                    "provider": account.provider,
                    "provider_email": account.provider_email,
                    "created_at": (account.created_at.isoformat() if account.created_at else None),
                    "last_used_at": (
                        account.last_used_at.isoformat() if account.last_used_at else None
                    ),
                }
                for account in accounts
            ]

    def unlink_oauth_account(self, user_id: str, oauth_account_id: str) -> bool:
        """Unlink OAuth account from user.

        Args:
            user_id: User ID
            oauth_account_id: OAuth account ID to unlink

        Returns:
            True if unlinked successfully

        Raises:
            ValueError: If account not found or doesn't belong to user
        """
        with self.session_factory() as session, session.begin():
            account = session.get(UserOAuthAccountModel, oauth_account_id)
            if not account:
                raise ValueError("OAuth account not found")

            if account.user_id != user_id:
                raise ValueError("OAuth account does not belong to user")

            session.delete(account)
            session.flush()

            logger.info(f"Unlinked OAuth account: {account.provider} from user {user_id}")
            return True
