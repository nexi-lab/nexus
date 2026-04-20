"""Provider-agnostic OAuth user authentication (Issue #1399).

Handles OAuth-based user login (distinct from backend integrations).
Accepts any provider via DI: ``dict[str, OAuthProviderProtocol]``.
"""

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from nexus.bricks.auth.oauth.crypto import OAuthCrypto
from nexus.bricks.auth.oauth.protocol import OAuthProviderProtocol
from nexus.bricks.auth.oauth.types import OAuthError
from nexus.bricks.auth.providers.local import LocalAuth
from nexus.storage.models import UserModel, UserOAuthAccountModel

if TYPE_CHECKING:
    from nexus.bricks.auth.protocols.user_provisioner import UserProvisionerProtocol

logger = logging.getLogger(__name__)

# Per-provider userinfo endpoints
_USERINFO_URLS: dict[str, str] = {
    "google": "https://www.googleapis.com/oauth2/v3/userinfo",
    "microsoft": "https://graph.microsoft.com/v1.0/me",
}


class OAuthUserAuth:
    """Provider-agnostic OAuth authentication for user login.

    Supports Google, Microsoft, and any provider via DI.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        providers: dict[str, OAuthProviderProtocol],
        *,
        jwt_secret: str | None = None,
        token_expiry: int = 3600,
        oauth_crypto: OAuthCrypto | None = None,
        user_provisioner: "UserProvisionerProtocol | None" = None,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.jwt_secret = jwt_secret or secrets.token_urlsafe(32)
        self.token_expiry = token_expiry
        self.oauth_crypto = oauth_crypto or OAuthCrypto()
        self.local_auth = LocalAuth(jwt_secret=self.jwt_secret, token_expiry=token_expiry)
        self._user_provisioner = user_provisioner

        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "Initialized OAuthUserAuth with providers: %s",
                list(providers.keys()),
            )

    def get_auth_url(self, provider_name: str, redirect_uri: str | None = None) -> tuple[str, str]:
        """Get OAuth authorization URL for any registered provider.

        Returns:
            Tuple of (authorization_url, state)
        """
        provider = self._get_provider(provider_name)
        state = secrets.token_urlsafe(32)
        kwargs: dict[str, Any] = {"state": state}
        if redirect_uri is not None:
            kwargs["redirect_uri"] = redirect_uri
        auth_url = provider.get_authorization_url(**kwargs)
        return auth_url, state

    # Keep backward-compat convenience method
    def get_google_auth_url(self, redirect_uri: str | None = None) -> tuple[str, str]:
        return self.get_auth_url("google", redirect_uri=redirect_uri)

    async def handle_callback(
        self,
        provider_name: str,
        code: str,
        state: str | None = None,
        redirect_uri: str | None = None,
        expected_state: str | None = None,
    ) -> tuple[UserModel, str]:
        """Handle OAuth callback for any provider.

        Args:
            provider_name: OAuth provider name.
            code: Authorization code from provider.
            state: State parameter returned in the callback.
            redirect_uri: Redirect URI used in the original request.
            expected_state: The state value originally sent in the auth URL.
                Must match ``state`` when provided (CSRF protection).

        Raises:
            ValueError: If state validation fails.
        """
        # Validate OAuth state for CSRF protection
        if expected_state is not None and (
            state is None or not secrets.compare_digest(state, expected_state)
        ):
            raise ValueError("OAuth state mismatch — possible CSRF attack")

        provider = self._get_provider(provider_name)

        try:
            kwargs: dict[str, Any] = {}
            if redirect_uri is not None:
                kwargs["redirect_uri"] = redirect_uri
            oauth_credential = await provider.exchange_code(code, **kwargs)
        except Exception as e:
            logger.error("Failed to exchange OAuth code for %s: %s", provider_name, e)
            raise ValueError(f"OAuth token exchange failed: {e}") from e

        user_info = await self._extract_user_info(provider_name, oauth_credential.access_token)

        provider_user_id = user_info.get("sub") or user_info.get("id")
        provider_email = (
            user_info.get("email") or user_info.get("mail") or user_info.get("userPrincipalName")
        )
        email_verified = user_info.get("email_verified", False)
        name = user_info.get("name") or user_info.get("displayName")
        picture = user_info.get("picture")

        if not provider_user_id:
            raise ValueError("OAuth response missing user ID claim")

        # Normalize provider base name (e.g. "google-auth" -> "google")
        base_provider = provider_name.split("-")[0]

        with self.session_factory() as session:
            user, is_new = await self._get_or_create_oauth_user(
                session=session,
                provider=base_provider,
                provider_user_id=str(provider_user_id),
                provider_email=provider_email,
                email_verified=email_verified,
                name=name,
                picture=picture,
                oauth_credential=oauth_credential,
            )

            user_info_dict = {
                "subject_type": "user",
                "subject_id": user.user_id,
                "zone_id": None,
                "is_admin": user.is_global_admin == 1,
                "name": user.display_name or user.username or user.email,
            }

            email_for_token = user.email or provider_email
            assert email_for_token is not None, "Email is required for token creation"
            token = self.local_auth.create_token(email_for_token, user_info_dict)

            if is_new:
                logger.info(
                    "Created new user from %s OAuth: %s (user_id=%s)",
                    provider_name,
                    provider_email,
                    user.user_id,
                )
            else:
                logger.info(
                    "Logged in existing user via %s OAuth: %s (user_id=%s)",
                    provider_name,
                    provider_email,
                    user.user_id,
                )

            return user, token

    # Keep backward-compat convenience method
    async def handle_google_callback(
        self,
        code: str,
        state: str | None = None,
        redirect_uri: str | None = None,
        expected_state: str | None = None,
    ) -> tuple[UserModel, str]:
        return await self.handle_callback(
            "google",
            code,
            state=state,
            redirect_uri=redirect_uri,
            expected_state=expected_state,
        )

    async def _extract_user_info(self, provider_name: str, access_token: str) -> dict[str, Any]:
        """Fetch user info from provider's userinfo endpoint."""
        import httpx

        base_provider = provider_name.split("-")[0]
        userinfo_url = _USERINFO_URLS.get(base_provider)
        if not userinfo_url:
            raise ValueError(f"No userinfo URL configured for provider: {provider_name}")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except Exception as e:
            logger.error("Failed to fetch userinfo for %s: %s", provider_name, e)
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
        """Get existing user or create from OAuth (with race condition protection).

        The new-user path defers ``_provision_oauth_user`` until after the outer
        transaction commits. ``UserProvisionerProtocol`` opens a second Session
        on the same engine and writes to ``users`` / ``zones``; invoking it
        inside the outer ``session.begin()`` block makes the inner INSERT wait
        on the still-pending users row lock, which deadlocks the request.
        """
        from nexus.bricks.auth.user_queries import get_user_by_email

        new_user_to_provision: UserModel | None = None

        with session.begin():
            stmt = select(UserOAuthAccountModel).where(
                UserOAuthAccountModel.provider == provider,
                UserOAuthAccountModel.provider_user_id == provider_user_id,
            )

            existing_oauth = session.scalar(stmt)

            if existing_oauth:
                user = session.get(UserModel, existing_oauth.user_id)
                if not user or user.is_active == 0:
                    raise ValueError("User account is inactive")

                existing_oauth.last_used_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(existing_oauth)
                session.flush()
                session.expunge(user)
                return user, False

            existing_user = None
            if provider_email and email_verified:
                existing_user = get_user_by_email(session, provider_email)

            if existing_user and existing_user.email_verified == 1:
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
                    session.expunge(existing_user)
                    logger.info("Linked OAuth account to existing user: %s", provider_email)
                    return existing_user, False
                except IntegrityError:
                    return self._retry_oauth_race(session, stmt)

            elif existing_user and existing_user.email_verified == 0:
                # Issue #3062: Block OAuth signup when an unverified local
                # account exists with the same email.  This prevents
                # duplicate-account confusion and pre-account-takeover
                # attacks (see CVE-2024-38351).
                raise ValueError(
                    "An account with this email already exists but is not verified. "
                    "Please verify your existing account first."
                )

            user_id = str(uuid.uuid4())
            user = UserModel(
                user_id=user_id,
                email=provider_email,
                username=None,
                display_name=name
                or (provider_email.split("@")[0] if provider_email else "OAuth User"),
                avatar_url=picture,
                password_hash=None,
                primary_auth_method="oauth",
                is_global_admin=0,
                is_active=1,
                email_verified=1 if email_verified else 0,
                user_metadata=None,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )

            session.add(user)
            session.flush()

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
                session.expunge(user)
                new_user_to_provision = user
            except IntegrityError:
                return self._retry_oauth_race(session, stmt)

        # Transaction has committed. Safe to invoke the user provisioner — it
        # opens its own Session and will not deadlock on our released locks.
        if new_user_to_provision is not None and new_user_to_provision.email:
            await self._provision_oauth_user(
                user_id=new_user_to_provision.user_id,
                email=new_user_to_provision.email,
                display_name=new_user_to_provision.display_name,
            )

        assert new_user_to_provision is not None
        return new_user_to_provision, True

    @staticmethod
    def _retry_oauth_race(
        session: Session,
        stmt: Any,
    ) -> tuple[UserModel, bool]:
        """Handle IntegrityError from a concurrent OAuth account creation race.

        Rolls back, re-queries for the winning OAuth account, and returns
        the associated user.  Extracted to avoid duplicating this pattern
        in the link-existing-user and create-new-user code paths.
        """
        session.rollback()
        existing_oauth = session.scalar(stmt)
        if existing_oauth:
            user = session.get(UserModel, existing_oauth.user_id)
            if not user:
                raise ValueError("User not found for OAuth account")
            session.expunge(user)
            return user, False
        raise  # re-raise original IntegrityError if no OAuth row found

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
        encrypted_id_token = None
        if hasattr(oauth_credential, "id_token") and oauth_credential.id_token:
            encrypted_id_token = self.oauth_crypto.encrypt_token(oauth_credential.id_token)

        metadata = getattr(oauth_credential, "metadata", None)
        provider_profile = json.dumps(
            {
                "name": metadata.get("name") if metadata else None,
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
                if oauth_credential and hasattr(oauth_credential, "expires_at")
                else None
            ),
            provider_profile=provider_profile,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            last_used_at=datetime.now(UTC).replace(tzinfo=None),
        )

        session.add(oauth_account)
        return oauth_account

    async def _provision_oauth_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None,
    ) -> None:
        if self._user_provisioner is None:
            logger.error(
                "Cannot provision OAuth user: no UserProvisionerProtocol injected. "
                "User created but missing zone, directories, workspace, agents, skills, API key."
            )
            return

        zone_id = email.split("@")[0] if email else user_id

        try:
            result = await self._user_provisioner.provision_user(
                user_id=user_id,
                email=email,
                display_name=display_name,
                zone_id=zone_id,
                create_api_key=True,
                create_agents=True,
                import_skills=False,
            )

            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Successfully provisioned OAuth user: user_id=%s, zone_id=%s",
                    user_id,
                    result["zone_id"],
                )

        except Exception as e:
            logger.error(
                "Failed to provision OAuth user resources (user_id=%s): %s",
                user_id,
                e,
                exc_info=True,
            )

    def get_user_oauth_accounts(self, user_id: str) -> list[dict[str, Any]]:
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
        with self.session_factory() as session, session.begin():
            account = session.get(UserOAuthAccountModel, oauth_account_id)
            if not account:
                raise ValueError("OAuth account not found")
            if account.user_id != user_id:
                raise ValueError("OAuth account does not belong to user")
            session.delete(account)
            session.flush()
            logger.info("Unlinked OAuth account: %s from user %s", account.provider, user_id)
            return True

    def _get_provider(self, provider_name: str) -> OAuthProviderProtocol:
        # Check exact match first, then base name match
        if provider_name in self.providers:
            return self.providers[provider_name]
        # Try matching base name (e.g. "google" matches "google-auth")
        for name, provider in self.providers.items():
            if name.startswith(provider_name) or provider_name.startswith(name):
                return provider
        available = list(self.providers.keys())
        raise OAuthError(f"OAuth provider '{provider_name}' not registered. Available: {available}")
