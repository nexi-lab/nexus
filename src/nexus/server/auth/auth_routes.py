"""Authentication API routes.

Provides endpoints for user registration, login, OAuth authentication, and profile management.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.oauth_user_auth import OAuthUserAuth

logger = logging.getLogger(__name__)

# ==============================================================================
# Dependency Injection
# ==============================================================================

_auth_provider: DatabaseLocalAuth | None = None
_oauth_provider: OAuthUserAuth | None = None
_nexus_fs_instance: Any | None = None  # NexusFS instance for provisioning


def set_auth_provider(provider: DatabaseLocalAuth) -> None:
    """Set the authentication provider for dependency injection."""
    global _auth_provider
    _auth_provider = provider


def get_auth_provider() -> DatabaseLocalAuth:
    """Get the authentication provider.

    Raises:
        HTTPException: If auth provider is not configured
    """
    if _auth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication provider not configured",
        )
    return _auth_provider


def set_oauth_provider(provider: OAuthUserAuth) -> None:
    """Set the OAuth authentication provider for dependency injection."""
    global _oauth_provider
    _oauth_provider = provider


def get_oauth_provider() -> OAuthUserAuth | None:
    """Get the OAuth authentication provider (optional)."""
    return _oauth_provider


def set_nexus_instance(nexus_fs: Any) -> None:
    """Set the global NexusFS instance for user provisioning."""
    global _nexus_fs_instance
    _nexus_fs_instance = nexus_fs


def get_nexus_instance() -> Any | None:
    """Get the global NexusFS instance."""
    return _nexus_fs_instance


async def get_authenticated_user(
    authorization: str = Header(..., description="Bearer JWT token"),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> tuple[str, str]:
    """Extract authenticated user from JWT token.

    Args:
        authorization: Authorization header with Bearer token
        auth: Authentication provider

    Returns:
        Tuple of (user_id, email)

    Raises:
        HTTPException: If token is missing, invalid, or expired
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'",
        )

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        claims = auth.verify_token(token)
        user_id = claims.get("subject_id")
        email = claims.get("email")

        if not user_id or not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user information",
            )

        return user_id, email
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
        ) from e


# ==============================================================================
# Request/Response Models
# ==============================================================================


class RegisterRequest(BaseModel):
    """User registration request."""

    email: EmailStr
    password: str = Field(..., min_length=8, description="Password must be at least 8 characters")
    username: str | None = Field(None, description="Optional username")
    display_name: str | None = Field(None, description="Optional display name")


class LoginRequest(BaseModel):
    """User login request."""

    identifier: str = Field(..., description="Email or username")
    password: str


class ChangePasswordRequest(BaseModel):
    """Change password request."""

    current_password: str
    new_password: str = Field(
        ..., min_length=8, description="New password must be at least 8 characters"
    )


class UpdateProfileRequest(BaseModel):
    """Update user profile request."""

    display_name: str | None = None
    avatar_url: str | None = None


class OAuthCallbackRequest(BaseModel):
    """OAuth callback request."""

    provider: str = Field(..., description="OAuth provider (e.g., 'google')")
    code: str = Field(..., description="Authorization code from OAuth provider")
    state: str | None = Field(None, description="State parameter for CSRF protection")
    redirect_uri: str | None = Field(None, description="Redirect URI used in authorization URL")


class OAuthCheckRequest(BaseModel):
    """OAuth check request."""

    provider: str = Field(..., description="OAuth provider (e.g., 'google')")
    code: str = Field(..., description="Authorization code from OAuth provider")
    state: str | None = Field(None, description="State parameter for CSRF protection")
    redirect_uri: str | None = Field(None, description="Redirect URI used in authorization URL")


class OAuthConfirmRequest(BaseModel):
    """OAuth confirmation request."""

    pending_token: str = Field(..., description="Pending token from OAuth check")
    tenant_name: str | None = Field(None, description="Optional tenant name for new user")
    tenant_slug: str | None = Field(None, description="Optional tenant slug for new user")


class TenantSetupRequest(BaseModel):
    """Tenant setup request for password users."""

    tenant_name: str | None = Field(None, description="Optional tenant name")
    tenant_slug: str | None = Field(None, description="Optional tenant slug")


class UserResponse(BaseModel):
    """User information response."""

    user_id: str
    email: str
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    is_global_admin: bool = False
    primary_auth_method: str | None = None


class RegisterResponse(BaseModel):
    """User registration response."""

    user_id: str
    email: str
    username: str | None = None
    display_name: str | None = None
    token: str
    api_key: str | None = None


class LoginResponse(BaseModel):
    """User login response."""

    token: str
    user: UserResponse
    api_key: str | None = None


class OAuthAuthorizeResponse(BaseModel):
    """OAuth authorization URL response."""

    auth_url: str
    state: str
    message: str = "Redirect user to auth_url to begin OAuth flow"


class OAuthCallbackResponse(BaseModel):
    """OAuth callback response."""

    token: str
    user: UserResponse
    is_new_user: bool
    message: str = "OAuth authentication successful"


class OAuthAccountResponse(BaseModel):
    """OAuth account information."""

    oauth_account_id: str
    provider: str
    provider_email: str
    created_at: str
    last_used_at: str | None = None


class OAuthCheckResponseExisting(BaseModel):
    """OAuth check response for existing users."""

    needs_confirmation: bool = False
    token: str
    user: UserResponse
    is_new_user: bool
    api_key: str | None = None
    tenant_id: str | None = None
    message: str = "OAuth authentication successful"


class OAuthCheckResponseNew(BaseModel):
    """OAuth check response for new users requiring confirmation."""

    needs_confirmation: bool = True
    pending_token: str
    user_info: dict[str, Any]
    tenant_info: dict[str, Any]
    message: str = "Please confirm your account details"


class OAuthConfirmResponse(BaseModel):
    """OAuth confirmation response."""

    token: str
    user: UserResponse
    is_new_user: bool
    api_key: str | None = None
    tenant_id: str | None = None
    message: str = "OAuth authentication confirmed"


class TenantSetupResponse(BaseModel):
    """Tenant setup response."""

    user: UserResponse
    api_key: str
    tenant_id: str
    message: str = "Tenant created successfully"


# ==============================================================================
# Router
# ==============================================================================

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest, auth: DatabaseLocalAuth = Depends(get_auth_provider)
) -> RegisterResponse:
    """Register a new user.

    Args:
        request: Registration request
        auth: Authentication provider

    Returns:
        User information and JWT token

    Raises:
        400: Email or username already exists
        422: Invalid request data
    """
    try:
        user, token = await auth.register(
            email=request.email,
            password=request.password,
            username=request.username,
            display_name=request.display_name,
        )

        # Don't create API key yet - user needs to create tenant first
        # Frontend will redirect to tenant creation page, then user can create API key
        api_key = None

        # User email should never be None after registration
        assert user.email is not None, "User email cannot be None after registration"

        return RegisterResponse(
            user_id=user.user_id,
            email=user.email,
            username=user.username,
            display_name=user.display_name,
            token=token,
            api_key=api_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest, auth: DatabaseLocalAuth = Depends(get_auth_provider)
) -> LoginResponse:
    """Login with email/username and password.

    Args:
        request: Login request
        auth: Authentication provider

    Returns:
        JWT token and user information

    Raises:
        401: Invalid credentials
    """
    try:
        result = await auth.login_async(identifier=request.identifier, password=request.password)
        if result is None:
            raise ValueError("Invalid email/username or password")

        user, token = result

        # User email should never be None after successful login
        assert user.email is not None, "User email cannot be None after login"

        # Try to retrieve encrypted API key for returning users
        # If user has already created tenant, they'll have an encrypted API key stored
        api_key = None
        oauth_provider = get_oauth_provider()

        if oauth_provider:
            from datetime import UTC, datetime

            from sqlalchemy import select

            from nexus.storage.models import APIKeyModel, OAuthAPIKeyModel

            with auth.session_factory() as session:
                # Look for encrypted API keys for this user
                api_key_stmt = select(OAuthAPIKeyModel).where(
                    OAuthAPIKeyModel.user_id == user.user_id,
                )
                oauth_api_keys = session.scalars(api_key_stmt).all()

                # Try to find a valid (non-expired, non-revoked) API key
                crypto = oauth_provider.oauth_crypto
                for oauth_key in oauth_api_keys:
                    try:
                        # Verify the key still exists in api_keys and hasn't expired or been revoked
                        api_key_model = session.get(APIKeyModel, oauth_key.key_id)
                        if api_key_model and not api_key_model.revoked:
                            # Check expiration
                            is_expired = False
                            if api_key_model.expires_at:
                                current_time = datetime.now(UTC)
                                expires_at = api_key_model.expires_at
                                if expires_at.tzinfo is None:
                                    expires_at = expires_at.replace(tzinfo=UTC)
                                is_expired = expires_at <= current_time

                            if not is_expired:
                                # Decrypt and return the API key
                                api_key = crypto.decrypt_token(oauth_key.encrypted_key_value)
                                break
                    except Exception as e:
                        # Decryption failed or key invalid, continue to next one
                        logger.warning(f"Failed to decrypt API key {oauth_key.key_id}: {e}")
                        continue

        return LoginResponse(
            token=token,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                username=user.username,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_global_admin=user.is_global_admin == 1,
                primary_auth_method=user.primary_auth_method,
            ),
            api_key=api_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e


@router.post("/setup-tenant", response_model=TenantSetupResponse)
async def setup_tenant(
    request: TenantSetupRequest,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> TenantSetupResponse:
    """Create tenant and API key for password-authenticated users.

    This endpoint is for password users who have already registered and logged in
    but don't have a tenant yet. It creates a tenant and generates an API key.

    Args:
        request: Tenant setup request with optional tenant_name and tenant_slug
        user_info: Authenticated user information from JWT token
        auth: Authentication provider

    Returns:
        User information, API key, and tenant_id

    Raises:
        401: Not authenticated or invalid token
        400: Invalid request data
        500: Server error during tenant creation
    """
    user_id, email = user_info

    try:
        from datetime import UTC, datetime, timedelta

        from nexus.server.auth.user_helpers import get_user_by_id

        # Get user from database
        with auth.session_factory() as session:
            user = get_user_by_id(session, user_id)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User not found: {user_id}",
                )

            if not user.email:
                raise ValueError("User email is required for tenant setup")

            # Generate tenant_id based on email type
            # For personal emails (gmail, outlook, etc): use username
            # For work emails: use full domain (e.g., multifi.ai)
            email_username, email_domain = (
                user.email.split("@") if "@" in user.email else (user.email, "")
            )
            personal_domains = [
                "gmail.com",
                "outlook.com",
                "hotmail.com",
                "yahoo.com",
                "icloud.com",
                "proton.me",
                "protonmail.com",
            ]
            is_personal = email_domain.lower() in personal_domains

            # Calculate default tenant_id and name
            if is_personal:
                default_tenant_id = email_username
                first_name = (
                    user.display_name.split()[0]
                    if user.display_name
                    else email_username.capitalize()
                )
                default_tenant_name = f"{first_name}'s Org"
            else:
                # Remove dots from domain for tenant_id (e.g., multifi.ai -> multifiai)
                default_tenant_id = email_domain.replace(".", "")
                default_tenant_name = email_domain

            # Use custom tenant_slug and tenant_name from request if provided
            tenant_id = request.tenant_slug if request.tenant_slug else default_tenant_id
            tenant_name = request.tenant_name if request.tenant_name else default_tenant_name

            # Make user detached so we can access it after session closes
            session.expunge(user)

        # Provision full user resources (workspace, agents, skills, permissions)
        # This is done outside the session to avoid conflicts
        api_key_value = None
        key_id = None
        try:
            from nexus.core.permissions import OperationContext

            nx = get_nexus_instance()
            if not nx:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="NexusFS instance not configured. Cannot provision user resources.",
                )

            admin_context = OperationContext(
                user="system",
                groups=[],
                tenant_id=tenant_id,
                is_admin=True,
            )

            # Provision user resources with API key (90 days expiry)
            provision_result = nx.provision_user(
                user_id=user_id,
                email=user.email,
                display_name=user.display_name,
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                create_api_key=True,
                api_key_name="Password Auth Auto-generated Key",
                api_key_expires_at=datetime.now(UTC) + timedelta(days=90),
                create_agents=True,
                import_skills=True,
                context=admin_context,
            )
            logger.info(f"Provisioned password user resources: {provision_result}")

            # Extract API key and key_id for encryption storage
            api_key_value = provision_result.get("api_key")
            key_id = provision_result.get("key_id")

            if not api_key_value or not key_id:
                raise ValueError("Failed to create API key during provisioning")

        except Exception as e:
            logger.error(f"Failed to provision password user resources: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to provision user resources: {e}",
            ) from e

        # Encrypt and store the raw API key in oauth_api_keys table
        # This allows password users to retrieve their API key on subsequent logins (if we implement that)
        try:
            from nexus.storage.models import OAuthAPIKeyModel

            # Get OAuth provider for crypto (password users use the same encryption as OAuth)
            oauth_provider = get_oauth_provider()
            if oauth_provider:
                crypto = oauth_provider.oauth_crypto
                encrypted_key_value = crypto.encrypt_token(api_key_value)

                # Store encrypted key in a new session
                with auth.session_factory() as key_session, key_session.begin():
                    oauth_api_key = OAuthAPIKeyModel(
                        key_id=key_id,
                        user_id=user_id,
                        encrypted_key_value=encrypted_key_value,
                    )
                    key_session.add(oauth_api_key)
                    logger.info(f"Stored encrypted API key for password user: {user_id}")
            else:
                # OAuth provider not configured - skip encryption storage
                # User can still use the API key, just can't retrieve it on subsequent logins
                logger.warning(
                    f"OAuth provider not configured - skipping encrypted API key storage for user {user_id}"
                )
        except Exception as e:
            logger.error(f"Failed to encrypt and store API key: {e}")
            # Don't fail the request - user still got the API key, just can't retrieve it later

        return TenantSetupResponse(
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                username=user.username,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_global_admin=user.is_global_admin == 1,
                primary_auth_method=user.primary_auth_method,
            ),
            api_key=api_key_value,
            tenant_id=tenant_id,
            message="Tenant created successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tenant setup failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Tenant setup failed: {e}",
        ) from e


@router.get("/me", response_model=UserResponse)
async def get_profile(
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> UserResponse:
    """Get current user profile.

    Args:
        user_info: Authenticated user information from JWT token
        auth: Authentication provider

    Returns:
        User information

    Raises:
        401: Not authenticated
        404: User not found
    """
    from nexus.server.auth.user_helpers import get_user_by_id

    user_id, _email = user_info

    # Get user from database
    with auth.session_factory() as session:
        user = get_user_by_id(session, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {user_id}",
            )

        return UserResponse(
            user_id=user.user_id,
            email=user.email or "",
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_global_admin=user.is_global_admin == 1,
            primary_auth_method=user.primary_auth_method,
        )


@router.patch("/me", response_model=UserResponse)
async def update_profile(
    request: UpdateProfileRequest,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> UserResponse:
    """Update current user profile.

    Args:
        request: Profile update request
        user_info: Authenticated user information from JWT token
        auth: Authentication provider

    Returns:
        Updated user information

    Raises:
        401: Not authenticated
        404: User not found
    """
    from nexus.server.auth.user_helpers import get_user_by_id

    user_id, _email = user_info

    # Update user in database
    with auth.session_factory() as session, session.begin():
        user = get_user_by_id(session, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {user_id}",
            )

        # Update fields if provided
        if request.display_name is not None:
            user.display_name = request.display_name
        if request.avatar_url is not None:
            user.avatar_url = request.avatar_url

        session.flush()

        return UserResponse(
            user_id=user.user_id,
            email=user.email or "",
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_global_admin=user.is_global_admin == 1,
            primary_auth_method=user.primary_auth_method,
        )


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    user_info: tuple[str, str] = Depends(get_authenticated_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> dict[str, str]:
    """Change user password.

    Args:
        request: Password change request
        user_info: Authenticated user information from JWT token
        auth: Authentication provider

    Returns:
        Success message

    Raises:
        401: Not authenticated or invalid current password
        404: User not found
    """
    user_id, _email = user_info

    # Change password
    try:
        success = auth.change_password(
            user_id=user_id,
            old_password=request.current_password,
            new_password=request.new_password,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )

        return {"message": "Password changed successfully", "success": "true"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


# ==============================================================================
# OAuth Routes
# ==============================================================================


@router.get("/oauth/google/authorize", response_model=OAuthAuthorizeResponse)
async def get_google_oauth_url(redirect_uri: str | None = None) -> OAuthAuthorizeResponse:
    """Get Google OAuth authorization URL.

    Args:
        redirect_uri: Optional redirect URI to use after OAuth callback.
                     If not provided, uses default from OAuth provider config.

    Returns:
        Authorization URL and state for OAuth flow

    Raises:
        500: OAuth provider not configured
    """
    oauth_provider = get_oauth_provider()
    if oauth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth is not configured. Please set up OAuth provider.",
        )

    try:
        auth_url, state = oauth_provider.get_google_auth_url(redirect_uri=redirect_uri)
        return OAuthAuthorizeResponse(auth_url=auth_url, state=state)
    except Exception as e:
        logger.error(f"Failed to generate Google OAuth URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate OAuth URL: {e}",
        ) from e


@router.post("/oauth/check")
async def oauth_check(
    request: OAuthCheckRequest,
) -> OAuthCheckResponseExisting | OAuthCheckResponseNew:
    """Check OAuth callback and determine if confirmation is needed.

    This endpoint checks if the OAuth callback is for an existing user (can login immediately)
    or a new user (needs confirmation before account creation).

    Args:
        request: OAuth check request with code and state

    Returns:
        Either existing user response (with token) or new user response (with pending_token)

    Raises:
        400: Invalid OAuth code or state
        500: OAuth provider not configured
    """
    if request.provider != "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {request.provider}",
        )

    oauth_provider = get_oauth_provider()
    if oauth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth is not configured. Please set up OAuth provider.",
        )

    try:
        # Exchange code for tokens to get user info
        from nexus.server.auth.pending_oauth import get_pending_oauth_manager

        oauth_credential = await oauth_provider.google_provider.exchange_code(
            request.code, redirect_uri=request.redirect_uri
        )
        user_info = await oauth_provider._extract_google_user_info(oauth_credential.access_token)

        provider_user_id = user_info.get("sub")
        provider_email = user_info.get("email")
        email_verified = user_info.get("email_verified", False)
        name = user_info.get("name")
        picture = user_info.get("picture")

        if not provider_user_id:
            raise ValueError("OAuth response missing 'sub' claim (user ID)")

        # Check if OAuth account already exists (existing user)
        from nexus.server.auth.user_helpers import get_user_by_email
        from nexus.storage.models import UserOAuthAccountModel

        with oauth_provider.session_factory() as session:
            # Check for existing OAuth account
            from sqlalchemy import select

            stmt = select(UserOAuthAccountModel).where(
                UserOAuthAccountModel.provider == "google",
                UserOAuthAccountModel.provider_user_id == provider_user_id,
            )
            existing_oauth = session.scalar(stmt)

            if existing_oauth:
                # Existing OAuth account - login immediately
                from datetime import UTC, datetime, timedelta

                from nexus.server.auth.database_key import DatabaseAPIKeyAuth
                from nexus.storage.models import APIKeyModel, UserModel

                user = session.get(UserModel, existing_oauth.user_id)
                if not user or user.is_active == 0:
                    raise ValueError("User account is inactive")
                if not user.email:
                    raise ValueError("User email is required for OAuth authentication")

                # Generate JWT token
                user_info_dict = {
                    "subject_type": "user",
                    "subject_id": user.user_id,
                    "tenant_id": None,
                    "is_admin": user.is_global_admin == 1,
                    "name": user.display_name or user.username or user.email,
                }
                token = oauth_provider.local_auth.create_token(user.email, user_info_dict)

                # Generate tenant_id based on email type
                # For personal emails (gmail, outlook, etc): use username
                # For work emails: use full domain (e.g., multifi.ai)
                if user.email:
                    email_username, email_domain = (
                        user.email.split("@") if "@" in user.email else (user.email, "")
                    )
                    personal_domains = [
                        "gmail.com",
                        "outlook.com",
                        "hotmail.com",
                        "yahoo.com",
                        "icloud.com",
                        "proton.me",
                        "protonmail.com",
                    ]
                    if email_domain.lower() in personal_domains:
                        tenant_id = email_username  # Use username for personal emails (e.g., "joe")
                    else:
                        tenant_id = (
                            email_domain  # Use full domain for work emails (e.g., "multifi.ai")
                        )
                else:
                    tenant_id = f"user_{user.user_id[:8]}"

                # Try to retrieve encrypted API key from oauth_api_keys table
                from nexus.storage.models import OAuthAPIKeyModel

                # Use the OAuth crypto instance from the provider
                crypto = oauth_provider.oauth_crypto

                # First, check if user has ANY OAuth API keys at all
                api_key_stmt = select(OAuthAPIKeyModel).where(
                    OAuthAPIKeyModel.user_id == user.user_id,
                )
                oauth_api_keys = session.scalars(api_key_stmt).all()

                # Try to find a valid (non-expired, non-revoked) API key with encrypted value
                api_key_value = None
                for oauth_key in oauth_api_keys:
                    try:
                        # Verify the key still exists in api_keys and hasn't expired or been revoked
                        api_key_model = session.get(APIKeyModel, oauth_key.key_id)
                        if api_key_model and not api_key_model.revoked:
                            # Check expiration - handle both timezone-aware and naive datetimes
                            is_expired = False
                            if api_key_model.expires_at:
                                current_time = datetime.now(UTC)
                                # Ensure both are timezone-aware for comparison
                                expires_at = api_key_model.expires_at
                                if expires_at.tzinfo is None:
                                    # If expires_at is naive, assume UTC
                                    expires_at = expires_at.replace(tzinfo=UTC)
                                is_expired = expires_at <= current_time

                            if not is_expired:
                                # Decrypt and return the API key
                                api_key_value = crypto.decrypt_token(oauth_key.encrypted_key_value)
                                break
                    except Exception as e:
                        # Decryption failed or key invalid, continue to next one
                        logger.warning(f"Failed to decrypt API key {oauth_key.key_id}: {e}")
                        continue

                # Only create a NEW API key if user has NO oauth_api_keys entries at all
                # (First OAuth login for this user)
                if not oauth_api_keys:
                    # Create new API key (90 days expiry) with proper tenant_id
                    key_id, api_key_value = DatabaseAPIKeyAuth.create_key(
                        session,
                        user_id=user.user_id,
                        name="OAuth Auto-generated Key",
                        tenant_id=tenant_id,
                        is_admin=user.is_global_admin == 1,
                        expires_at=datetime.now(UTC) + timedelta(days=90),
                    )

                    # Encrypt and store the raw API key in oauth_api_keys table
                    encrypted_key_value = crypto.encrypt_token(api_key_value)
                    oauth_api_key = OAuthAPIKeyModel(
                        key_id=key_id,
                        user_id=user.user_id,
                        encrypted_key_value=encrypted_key_value,
                    )
                    session.add(oauth_api_key)
                    session.commit()
                    logger.info(f"Created first OAuth API key for user {user.user_id}")
                elif not api_key_value:
                    # User has oauth_api_keys but couldn't decrypt any - likely encryption key changed
                    # This shouldn't happen with persistent encryption key, but log it
                    logger.error(
                        f"User {user.user_id} has {len(oauth_api_keys)} OAuth API keys but none could be decrypted"
                    )
                    api_key_value = None

                return OAuthCheckResponseExisting(
                    needs_confirmation=False,
                    token=token,
                    user=UserResponse(
                        user_id=user.user_id,
                        email=user.email,
                        username=user.username,
                        display_name=user.display_name,
                        avatar_url=user.avatar_url,
                        is_global_admin=user.is_global_admin == 1,
                        primary_auth_method="oauth",
                    ),
                    is_new_user=False,
                    api_key=api_key_value,
                    tenant_id=tenant_id,
                )

            # Check if email already exists (can auto-link if both verified)
            existing_user = None
            if provider_email and email_verified:
                existing_user = get_user_by_email(session, provider_email)

            if existing_user and existing_user.email_verified == 1:
                # Email verified on both sides - auto-link and login
                # Note: Existing users will go through frontend tenant creation flow
                user, token = await oauth_provider.handle_google_callback(
                    code=request.code, _state=request.state
                )
                if not user.email:
                    raise ValueError("OAuth user must have an email")

                return OAuthCheckResponseExisting(
                    needs_confirmation=False,
                    token=token,
                    user=UserResponse(
                        user_id=user.user_id,
                        email=user.email,
                        username=user.username,
                        display_name=user.display_name,
                        avatar_url=user.avatar_url,
                        is_global_admin=user.is_global_admin == 1,
                        primary_auth_method="oauth",
                    ),
                    is_new_user=False,
                    api_key=None,  # Frontend will guide through tenant creation
                    tenant_id=None,  # Frontend will create tenant via UX
                )

        # New user - create pending registration with OAuth credential
        pending_manager = get_pending_oauth_manager()
        pending_token = pending_manager.create(
            provider="google",
            provider_user_id=provider_user_id,
            provider_email=provider_email,
            email_verified=email_verified,
            name=name,
            picture=picture,
            oauth_credential=oauth_credential,  # Store the credential, not the code
        )

        # Calculate smart tenant_id and tenant_name for preview
        if provider_email:
            email_username, email_domain = (
                provider_email.split("@") if "@" in provider_email else (provider_email, "")
            )
            personal_domains = [
                "gmail.com",
                "outlook.com",
                "hotmail.com",
                "yahoo.com",
                "icloud.com",
                "proton.me",
                "protonmail.com",
            ]
            is_personal = email_domain.lower() in personal_domains

            # Calculate proposed tenant_id and name
            if is_personal:
                proposed_tenant_id = email_username
                first_name = name.split()[0] if name else email_username.capitalize()
                proposed_tenant_name = f"{first_name}'s Org"
            else:
                # Remove dots from domain for tenant_id (e.g., multifi.ai -> multifiai)
                proposed_tenant_id = email_domain.replace(".", "")
                proposed_tenant_name = email_domain
        else:
            is_personal = True
            proposed_tenant_id = ""
            proposed_tenant_name = ""

        return OAuthCheckResponseNew(
            needs_confirmation=True,
            pending_token=pending_token,
            user_info={
                "email": provider_email,
                "display_name": name,
                "avatar_url": picture,
                "oauth_provider": "google",
                "email_verified": email_verified,
            },
            tenant_info={
                "tenant_id": proposed_tenant_id,
                "name": proposed_tenant_name,
                "domain": email_domain if provider_email else None,
                "description": None,
                "is_personal": is_personal,
                "can_edit_name": is_personal,  # Only personal orgs can edit name
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.error(f"OAuth check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth check failed: {e}",
        ) from e


@router.post("/oauth/confirm", response_model=OAuthConfirmResponse)
async def oauth_confirm(request: OAuthConfirmRequest) -> OAuthConfirmResponse:
    """Confirm OAuth registration for new users.

    Args:
        request: OAuth confirmation request with pending_token

    Returns:
        JWT token and user information

    Raises:
        400: Invalid pending token
        500: OAuth provider not configured
    """
    oauth_provider = get_oauth_provider()
    if oauth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth is not configured. Please set up OAuth provider.",
        )

    # Initialize variables that may be set in different code paths
    api_key_value: str | None = None
    key_id: str | None = None

    try:
        # Validate and consume pending token (one-time use)
        from nexus.server.auth.pending_oauth import get_pending_oauth_manager

        pending_manager = get_pending_oauth_manager()
        registration = pending_manager.consume(request.pending_token)

        if registration is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired pending token",
            )

        # Use the stored OAuth credential (no need to re-exchange the code)
        oauth_credential = registration.oauth_credential

        # Create user account with OAuth info
        import uuid
        from datetime import UTC, datetime, timedelta

        from nexus.server.auth.database_key import DatabaseAPIKeyAuth
        from nexus.server.auth.user_helpers import get_user_by_email
        from nexus.storage.models import UserModel

        # Check if user with this email already exists
        if not registration.provider_email:
            raise ValueError("Provider email is required")

        with oauth_provider.session_factory() as session:
            existing_user = get_user_by_email(session, registration.provider_email)
            if existing_user:
                if not existing_user.email:
                    raise ValueError("Existing user email is required")
                # User exists but not linked to OAuth - auto-link them
                user_id = existing_user.user_id
                tenant_id = "default"  # Use their existing tenant

                with session.begin():
                    # Create OAuth account link
                    await oauth_provider._create_oauth_account(
                        session=session,
                        user_id=user_id,
                        provider=registration.provider,
                        provider_user_id=registration.provider_user_id,
                        provider_email=registration.provider_email,
                        picture=registration.picture,
                        oauth_credential=oauth_credential,
                    )
                    session.flush()

                    # Generate API key for user
                    key_id, api_key_value = DatabaseAPIKeyAuth.create_key(
                        session,
                        user_id=user_id,
                        name="OAuth Auto-generated Key",
                        tenant_id=tenant_id,
                        is_admin=existing_user.is_global_admin == 1,
                        expires_at=datetime.now(UTC) + timedelta(days=90),
                    )

                user_info_dict = {
                    "subject_type": "user",
                    "subject_id": user_id,
                    "tenant_id": tenant_id,
                    "is_admin": existing_user.is_global_admin == 1,
                    "name": existing_user.display_name
                    or existing_user.username
                    or existing_user.email,
                }
                token = oauth_provider.local_auth.create_token(existing_user.email, user_info_dict)

                logger.info(
                    f"OAuth linked to existing user: {registration.provider_email} (user_id={user_id})"
                )

                return OAuthConfirmResponse(
                    token=token,
                    user=UserResponse(
                        user_id=existing_user.user_id,
                        email=existing_user.email,
                        username=existing_user.username,
                        display_name=existing_user.display_name,
                        avatar_url=existing_user.avatar_url,
                        is_global_admin=existing_user.is_global_admin == 1,
                        primary_auth_method="oauth",
                    ),
                    is_new_user=False,
                    api_key=api_key_value,
                    tenant_id=tenant_id,
                    message="OAuth linked to existing account",
                )

        # New user - create tenant, user, and OAuth account
        user_id = str(uuid.uuid4())

        # Generate tenant_id based on email type (same logic as existing users)
        # For personal emails: use username, for work emails: use full domain
        if registration.provider_email:
            email_username, email_domain = (
                registration.provider_email.split("@")
                if "@" in registration.provider_email
                else (registration.provider_email, "")
            )
            personal_domains = [
                "gmail.com",
                "outlook.com",
                "hotmail.com",
                "yahoo.com",
                "icloud.com",
                "proton.me",
                "protonmail.com",
            ]
            # Use email username for personal domains, domain for org domains
            default_tenant_id = (
                email_username if email_domain.lower() in personal_domains else email_domain
            )

            # Calculate smart tenant name based on email type
            if email_domain.lower() in personal_domains:
                # Personal: extract first name from display_name
                first_name = (
                    registration.name.split()[0]
                    if registration.name
                    else email_username.capitalize()
                )
                default_tenant_name = f"{first_name}'s Org"
            else:
                # Work: use domain
                default_tenant_name = email_domain
        else:
            default_tenant_id = f"user_{user_id[:8]}"
            default_tenant_name = f"User {user_id[:8]} Organization"

        # Use custom tenant_slug and tenant_name from frontend if provided, otherwise use defaults
        tenant_id = request.tenant_slug if request.tenant_slug else default_tenant_id
        tenant_name = request.tenant_name if request.tenant_name else default_tenant_name

        # Ensure provider email exists for new user creation
        if not registration.provider_email:
            raise ValueError("Provider email is required for user creation")

        with oauth_provider.session_factory() as session, session.begin():
            # Create user
            user = UserModel(
                user_id=user_id,
                email=registration.provider_email,
                username=None,
                display_name=registration.name or registration.provider_email.split("@")[0]
                if registration.provider_email
                else "OAuth User",
                avatar_url=registration.picture,
                password_hash=None,  # OAuth users don't have password
                primary_auth_method="oauth",
                is_global_admin=0,
                is_active=1,
                email_verified=1 if registration.email_verified else 0,
                user_metadata=None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(user)
            session.flush()

            # Create OAuth account
            await oauth_provider._create_oauth_account(
                session=session,
                user_id=user_id,
                provider=registration.provider,
                provider_user_id=registration.provider_user_id,
                provider_email=registration.provider_email,
                picture=registration.picture,
                oauth_credential=oauth_credential,
            )
            session.flush()

            # Make user detached so we can access it after session closes
            session.expunge(user)

        # Provision full user resources (workspace, agents, skills, permissions)
        # IMPORTANT: This only runs for NEW users - existing users return early above (line 763)
        # This is done outside the session to avoid conflicts
        api_key_value = None
        key_id = None
        try:
            from nexus.core.permissions import OperationContext

            nx = get_nexus_instance()
            if nx:
                admin_context = OperationContext(
                    user="system",
                    groups=[],
                    tenant_id=tenant_id,
                    is_admin=True,
                )

                # Provision user resources with OAuth-specific API key (90 days expiry)
                provision_result = nx.provision_user(
                    user_id=user_id,
                    email=user.email,
                    display_name=user.display_name,
                    tenant_id=tenant_id,
                    tenant_name=tenant_name,
                    create_api_key=True,
                    api_key_name="OAuth Auto-generated Key",
                    api_key_expires_at=datetime.now(UTC) + timedelta(days=90),
                    create_agents=True,
                    import_skills=True,
                    context=admin_context,
                )
                logger.info(f"Provisioned OAuth user resources: {provision_result}")

                # Extract API key and key_id for OAuth encryption
                api_key_value = provision_result.get("api_key")
                key_id = provision_result.get("key_id")

        except Exception as e:
            logger.error(f"Failed to provision OAuth user resources: {e}")
            # Continue - user can be provisioned later via retry

        # Encrypt and store the raw API key in oauth_api_keys table
        # This allows OAuth users to retrieve their API key on subsequent logins
        if api_key_value and key_id:
            try:
                from nexus.storage.models import OAuthAPIKeyModel

                # Use the OAuth crypto instance from the provider
                crypto = oauth_provider.oauth_crypto
                encrypted_key_value = crypto.encrypt_token(api_key_value)

                # Store encrypted key in a new session
                with oauth_provider.session_factory() as oauth_session, oauth_session.begin():
                    oauth_api_key = OAuthAPIKeyModel(
                        key_id=key_id,
                        user_id=user_id,
                        encrypted_key_value=encrypted_key_value,
                    )
                    oauth_session.add(oauth_api_key)
                    logger.info(f"Stored encrypted API key for OAuth user: {user_id}")
            except Exception as e:
                logger.error(f"Failed to encrypt and store OAuth API key: {e}")

        # Type guard: email is required for OAuth users
        assert user.email is not None, "OAuth user must have email"

        # Generate JWT token
        user_info_dict = {
            "subject_type": "user",
            "subject_id": user.user_id,
            "tenant_id": tenant_id,
            "is_admin": False,
            "name": user.display_name or user.username or user.email,
        }
        token = oauth_provider.local_auth.create_token(user.email, user_info_dict)

        logger.info(
            f"OAuth registration confirmed: {registration.provider_email} (user_id={user_id}, tenant={tenant_id})"
        )

        return OAuthConfirmResponse(
            token=token,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                username=user.username,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_global_admin=False,
                primary_auth_method="oauth",
            ),
            is_new_user=True,
            api_key=api_key_value,
            tenant_id=tenant_id,
            message="OAuth authentication confirmed and account created",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth confirm failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth confirm failed: {e}",
        ) from e


@router.post("/oauth/callback", response_model=OAuthCallbackResponse)
async def oauth_callback(request: OAuthCallbackRequest) -> OAuthCallbackResponse:
    """Handle OAuth callback.

    Exchanges authorization code for tokens and creates/links user account.

    Args:
        request: OAuth callback request with code and state

    Returns:
        JWT token and user information

    Raises:
        400: Invalid OAuth code or state
        500: OAuth provider not configured
    """
    if request.provider != "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {request.provider}",
        )

    oauth_provider = get_oauth_provider()
    if oauth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google OAuth is not configured. Please set up OAuth provider.",
        )

    try:
        user, token = await oauth_provider.handle_google_callback(
            code=request.code, _state=request.state, redirect_uri=request.redirect_uri
        )

        # Ensure user has email (required for OAuth)
        if not user.email:
            raise ValueError("OAuth user must have an email")

        # Determine if this is a new user (check if user was just created)
        # For now, we'll assume it's new if the user was created recently
        # This is a simplification - in practice, you'd track this in the handler
        is_new_user = False  # TODO: Track this properly in OAuthUserAuth

        return OAuthCallbackResponse(
            token=token,
            user=UserResponse(
                user_id=user.user_id,
                email=user.email,
                username=user.username,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_global_admin=user.is_global_admin == 1,
                primary_auth_method="oauth",
            ),
            is_new_user=is_new_user,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth callback failed: {e}",
        ) from e


@router.get("/oauth/accounts", response_model=list[OAuthAccountResponse])
async def list_oauth_accounts() -> list[OAuthAccountResponse]:
    """List linked OAuth accounts for current user.

    Returns:
        List of linked OAuth accounts

    Raises:
        401: Not authenticated
        501: Not implemented
    """
    # TODO: Implement OAuth account listing
    # Requires: JWT token authentication middleware
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth account listing requires JWT token authentication middleware",
    )


@router.delete("/oauth/accounts/{oauth_account_id}")
async def unlink_oauth_account(_oauth_account_id: str) -> dict[str, Any]:
    """Unlink an OAuth account.

    Args:
        oauth_account_id: OAuth account ID to unlink

    Returns:
        Success message

    Raises:
        401: Not authenticated
        404: OAuth account not found
        501: Not implemented
    """
    # TODO: Implement OAuth account unlinking
    # Requires: JWT token authentication middleware
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth account unlinking requires JWT token authentication middleware",
    )
