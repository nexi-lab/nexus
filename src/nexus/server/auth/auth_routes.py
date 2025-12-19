"""Authentication API routes for Nexus server.

Provides REST API endpoints for:
- User registration
- Login/logout
- Password management
- User profile management
- Email verification (TODO)
- Password reset (TODO)
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from nexus.server.auth.base import AuthResult
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.oauth_user_auth import OAuthUserAuth

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/auth", tags=["authentication"])

# Security scheme for Bearer token authentication
security = HTTPBearer()


# ==============================================================================
# Pydantic Models (Request/Response)
# ==============================================================================


class RegisterRequest(BaseModel):
    """User registration request."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=12, description="Password (min 12 characters)")
    username: str | None = Field(None, description="Optional username")
    display_name: str | None = Field(None, description="Optional display name")


class RegisterResponse(BaseModel):
    """User registration response."""

    user_id: str
    email: str
    username: str | None
    display_name: str | None
    token: str
    message: str = "User registered successfully"


class LoginRequest(BaseModel):
    """User login request."""

    identifier: str = Field(..., description="Email or username")
    password: str = Field(..., description="Password")


class LoginResponse(BaseModel):
    """User login response."""

    token: str
    user: dict[str, Any]
    message: str = "Login successful"


class ChangePasswordRequest(BaseModel):
    """Change password request."""

    old_password: str = Field(..., description="Current password")
    new_password: str = Field(..., min_length=12, description="New password (min 12 characters)")


class UpdateProfileRequest(BaseModel):
    """Update user profile request."""

    display_name: str | None = Field(None, description="Display name")
    avatar_url: str | None = Field(None, description="Avatar URL")
    metadata: dict[str, Any] | None = Field(None, description="Additional metadata")


class UserInfoResponse(BaseModel):
    """User information response."""

    user_id: str
    email: str | None
    username: str | None
    display_name: str | None
    avatar_url: str | None
    primary_auth_method: str
    is_global_admin: bool
    email_verified: bool
    api_key: str | None = None
    tenant_id: str | None = None
    created_at: str | None
    last_login_at: str | None


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str
    success: bool = True


# ==============================================================================
# Dependency Injection
# ==============================================================================


# This will be set by the FastAPI app initialization
_auth_provider: DatabaseLocalAuth | None = None
_oauth_provider: OAuthUserAuth | None = None


def set_auth_provider(auth_provider: DatabaseLocalAuth) -> None:
    """Set the auth provider for dependency injection.

    Call this from your FastAPI app startup:
        set_auth_provider(DatabaseLocalAuth(session_factory, jwt_secret))
    """
    global _auth_provider
    _auth_provider = auth_provider


def set_oauth_provider(oauth_provider: OAuthUserAuth) -> None:
    """Set the OAuth provider for dependency injection.

    Call this from your FastAPI app startup:
        set_oauth_provider(OAuthUserAuth(session_factory, ...))
    """
    global _oauth_provider
    _oauth_provider = oauth_provider


def get_auth_provider() -> DatabaseLocalAuth:
    """Get auth provider dependency."""
    if _auth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication provider not initialized",
        )
    return _auth_provider


def get_oauth_provider() -> OAuthUserAuth:
    """Get OAuth provider dependency."""
    if _oauth_provider is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth provider not initialized",
        )
    return _oauth_provider


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> AuthResult:
    """Get current authenticated user from Bearer token in Authorization header.

    Args:
        credentials: HTTP Bearer token credentials extracted from Authorization header
        auth: Database authentication provider

    Returns:
        AuthResult with user information

    Raises:
        HTTPException: If token is invalid or expired
    """
    token = credentials.credentials

    result = await auth.authenticate(token)
    if not result.authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return result


# ==============================================================================
# Registration & Login Endpoints
# ==============================================================================


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> RegisterResponse:
    """Register a new user account.

    Creates a new user with email/password authentication.
    Returns a JWT token for immediate login.

    Note: Email verification is not yet implemented (TODO).
    Note: Tenant membership must be added separately via ReBAC API.
    """
    try:
        # Register user
        user = auth.register_user(
            email=request.email,
            password=request.password,
            username=request.username,
            display_name=request.display_name,
        )

        # Create login token
        token = auth.login(request.email, request.password)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create token after registration",
            )

        return RegisterResponse(
            user_id=user.user_id,
            email=user.email,
            username=user.username,
            display_name=user.display_name,
            token=token,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> LoginResponse:
    """Login with email/username and password.

    Returns a JWT token valid for 1 hour (configurable).
    """
    token = auth.login(request.identifier, request.password)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user info from token claims
    try:
        claims = auth.verify_token(token)
        user_info = auth.get_user_info(claims.get("subject_id"))
        if not user_info:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve user info",
            )

        return LoginResponse(
            token=token,
            user=user_info,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token generation error: {e}",
        )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    current_user: AuthResult = Depends(get_current_user),
) -> MessageResponse:
    """Logout current user.

    Note: JWT tokens are stateless, so logout is client-side only.
    The client should discard the token. For server-side logout,
    implement token blacklisting (TODO).
    """
    # TODO: Implement server-side token blacklisting
    return MessageResponse(
        message="Logged out successfully. Please discard your token.",
    )


# ==============================================================================
# User Profile Endpoints
# ==============================================================================


@router.get("/me", response_model=UserInfoResponse)
async def get_current_user_info(
    current_user: AuthResult = Depends(get_current_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> UserInfoResponse:
    """Get current user information."""
    user_info = auth.get_user_info(current_user.subject_id)
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserInfoResponse(**user_info)


@router.patch("/me", response_model=UserInfoResponse)
async def update_current_user_profile(
    request: UpdateProfileRequest,
    current_user: AuthResult = Depends(get_current_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> UserInfoResponse:
    """Update current user profile."""
    user = auth.update_profile(
        user_id=current_user.subject_id,
        display_name=request.display_name,
        avatar_url=request.avatar_url,
        metadata=request.metadata,
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user_info = auth.get_user_info(current_user.subject_id)
    return UserInfoResponse(**user_info)


# ==============================================================================
# Password Management Endpoints
# ==============================================================================


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    request: ChangePasswordRequest,
    current_user: AuthResult = Depends(get_current_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> MessageResponse:
    """Change password for current user."""
    try:
        auth.change_password(
            user_id=current_user.subject_id,
            old_password=request.old_password,
            new_password=request.new_password,
        )

        return MessageResponse(
            message="Password changed successfully",
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ==============================================================================
# OAuth Endpoints
# ==============================================================================


class OAuthProviderInfo(BaseModel):
    """OAuth provider information."""

    name: str
    display_name: str
    auth_url: str


class OAuthCallbackRequest(BaseModel):
    """OAuth callback request."""

    provider: str = Field(..., description="OAuth provider (e.g., 'google')")
    code: str = Field(..., description="Authorization code from OAuth callback")
    state: str | None = Field(None, description="State parameter for CSRF protection")


class OAuthCallbackResponse(BaseModel):
    """OAuth callback response."""

    token: str
    user: dict[str, Any]
    is_new_user: bool
    api_key: str | None = None  # API key for new users (self-serve tenant)
    tenant_id: str | None = None  # Tenant ID (email address)
    message: str = "OAuth authentication successful"
    needs_confirmation: bool = False  # True if user needs to confirm info before account creation


class PendingUserInfo(BaseModel):
    """Pending user information for confirmation."""

    email: str
    display_name: str | None
    avatar_url: str | None
    oauth_provider: str
    oauth_code: str
    oauth_state: str | None


class PendingTenantInfo(BaseModel):
    """Pending tenant information for confirmation."""

    tenant_id: str
    name: str
    domain: str | None
    description: str | None
    is_personal: bool
    can_edit_name: bool  # True if user can edit tenant name (personal workspaces only)


class OAuthConfirmationResponse(BaseModel):
    """OAuth confirmation data for new users."""

    needs_confirmation: bool = True
    pending_token: str  # Token to complete registration
    user_info: PendingUserInfo
    tenant_info: PendingTenantInfo
    message: str = "Please confirm user and tenant information"


class ConfirmUserRequest(BaseModel):
    """Request to confirm and create user after OAuth."""

    pending_token: str = Field(..., description="Pending registration token from /oauth/check")
    tenant_name: str | None = Field(None, description="Custom tenant name")
    tenant_slug: str | None = Field(None, description="Custom tenant slug")


class OAuthAccountInfo(BaseModel):
    """OAuth account information."""

    oauth_account_id: str
    provider: str
    provider_email: str | None
    created_at: str | None
    last_used_at: str | None


@router.get("/oauth/providers", response_model=list[OAuthProviderInfo])
async def list_oauth_providers(
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
) -> list[OAuthProviderInfo]:
    """List available OAuth providers.

    Returns information about configured OAuth providers for user authentication.
    """
    # For now, only Google is supported
    auth_url, state = oauth.get_google_auth_url()

    return [
        OAuthProviderInfo(
            name="google",
            display_name="Google",
            auth_url=auth_url,
        )
    ]


@router.get("/oauth/google/authorize")
async def get_google_oauth_url(
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
) -> dict[str, str]:
    """Get Google OAuth authorization URL.

    Returns:
        Dictionary with 'auth_url' and 'state' fields
        Client should redirect user to auth_url and store state in session
    """
    auth_url, state = oauth.get_google_auth_url()

    return {
        "auth_url": auth_url,
        "state": state,
        "message": "Redirect user to auth_url to begin OAuth flow",
    }


@router.post("/oauth/check")
async def check_oauth_user(
    request: OAuthCallbackRequest,
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> OAuthConfirmationResponse | OAuthCallbackResponse:
    """Check if OAuth user exists and needs confirmation.

    For new users, returns confirmation data with pending user and tenant info.
    For existing users, completes login and returns JWT token.
    """
    if request.provider != "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {request.provider}",
        )

    try:
        # Exchange code for tokens and get user info
        import httpx
        oauth_credential = await oauth.google_provider.exchange_code(request.code)
        user_info = await oauth._extract_google_user_info(oauth_credential.access_token)

        provider_user_id = user_info.get("sub")
        provider_email = user_info.get("email")
        name = user_info.get("name")
        picture = user_info.get("picture")

        if not provider_user_id or not provider_email:
            raise ValueError("OAuth response missing required user information")

        # Check if user already exists
        from nexus.storage.models import UserOAuthAccountModel

        with auth.session_factory() as session:
            existing_oauth = (
                session.query(UserOAuthAccountModel)
                .filter(
                    UserOAuthAccountModel.provider == "google",
                    UserOAuthAccountModel.provider_user_id == provider_user_id,
                )
                .first()
            )

            if existing_oauth:
                # User exists - complete login immediately
                # Don't exchange code again (already done above), just create JWT token
                user_info_dict = auth.get_user_info_for_jwt(existing_oauth.user_id)
                if not user_info_dict:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="User data not found",
                    )

                # Create JWT token for the existing user
                token = auth.create_token(
                    email=provider_email,
                    user_info=user_info_dict
                )

                return OAuthCallbackResponse(
                    token=token,
                    user=user_info_dict,
                    is_new_user=False,
                    api_key=user_info_dict.get("api_key"),
                    tenant_id=user_info_dict.get("tenant_id"),
                    needs_confirmation=False,
                )

            # New user - return confirmation data
            from nexus.server.auth.tenant_helpers import get_tenant_strategy_from_email

            (base_slug, tenant_name_base, email_domain, is_personal) = get_tenant_strategy_from_email(provider_email)

            # Build tenant info
            if is_personal:
                first_name = name.split()[0].strip() if name else None
                tenant_name = f"{first_name or tenant_name_base}'s Org"
                description = f"Personal organization for {name or provider_email}"
            else:
                tenant_name = tenant_name_base
                description = f"Organization for {email_domain}"

            # Create a pending token that encodes the OAuth user info and tokens
            # This token will be used to complete registration after confirmation
            # Store OAuth credential tokens (they've already been exchanged, can't reuse code)
            pending_data = {
                "provider": "google",
                "provider_user_id": provider_user_id,
                "provider_email": provider_email,
                "name": name,
                "picture": picture,
                "base_slug": base_slug,
                "tenant_name_base": tenant_name_base,
                "email_domain": email_domain,
                "is_personal": is_personal,
                # Store OAuth tokens for later user creation
                "oauth_tokens": {
                    "access_token": oauth_credential.access_token,
                    "refresh_token": oauth_credential.refresh_token,
                    "token_type": oauth_credential.token_type,
                    "expires_at": oauth_credential.expires_at.isoformat() if oauth_credential.expires_at else None,
                },
            }

            import json
            import time
            import jwt as pyjwt

            # Create custom JWT for pending registration (10 minute expiry)
            pending_payload = {
                "sub": f"pending:{provider_email}",
                "email": provider_email,
                "pending_oauth": json.dumps(pending_data),
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,  # 10 minutes to confirm
            }
            pending_token = pyjwt.encode(pending_payload, auth.jwt_secret, algorithm="HS256")
            if isinstance(pending_token, bytes):
                pending_token = pending_token.decode()

            return OAuthConfirmationResponse(
                needs_confirmation=True,
                pending_token=pending_token,
                user_info=PendingUserInfo(
                    email=provider_email,
                    display_name=name,
                    avatar_url=picture,
                    oauth_provider="google",
                    oauth_code=request.code,
                    oauth_state=request.state,
                ),
                tenant_info=PendingTenantInfo(
                    tenant_id=base_slug,
                    name=tenant_name,
                    domain=email_domain,
                    description=description,
                    is_personal=is_personal,
                    can_edit_name=is_personal,  # Only personal workspaces can be renamed
                ),
                message="Please confirm your information to complete registration",
            )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"OAuth check error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth check failed: {str(e)}",
        )


@router.post("/oauth/confirm", response_model=OAuthCallbackResponse)
async def confirm_oauth_user(
    request: ConfirmUserRequest,
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> OAuthCallbackResponse:
    """Confirm and complete user registration after OAuth.

    Takes the pending token from /oauth/check and creates the user account with confirmed tenant information.
    """
    import json
    from datetime import datetime, timezone

    try:
        # Verify and decode pending token
        token_payload = auth.verify_token(request.pending_token)
        if not token_payload or "pending_oauth" not in token_payload:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired pending registration token",
            )

        # Extract pending OAuth data
        pending_data = json.loads(token_payload["pending_oauth"])

        provider_user_id = pending_data["provider_user_id"]
        provider_email = pending_data["provider_email"]
        name = pending_data["name"]
        picture = pending_data["picture"]
        base_slug = pending_data["base_slug"]
        tenant_name_base = pending_data["tenant_name_base"]
        email_domain = pending_data["email_domain"]
        is_personal = pending_data["is_personal"]
        oauth_tokens = pending_data["oauth_tokens"]

        # Use custom tenant name if provided (only for personal organizations)
        if request.tenant_name and is_personal:
            tenant_name = request.tenant_name
        else:
            # Use default tenant name
            if is_personal:
                first_name = name.split()[0].strip() if name else None
                tenant_name = f"{first_name or tenant_name_base}'s Org"
            else:
                tenant_name = tenant_name_base

        # Now create the user, tenant, and OAuth account
        from nexus.core.entity_registry import EntityRegistry
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth
        from nexus.server.auth.tenant_helpers import create_tenant, normalize_to_slug, suggest_tenant_id
        from nexus.server.auth.user_helpers import add_user_to_tenant
        from nexus.storage.models import TenantModel, UserModel, UserOAuthAccountModel

        # Double-check user doesn't exist AND validate slug BEFORE creating anything
        with auth.session_factory() as session:
            existing_oauth = (
                session.query(UserOAuthAccountModel)
                .filter(
                    UserOAuthAccountModel.provider == "google",
                    UserOAuthAccountModel.provider_user_id == provider_user_id,
                )
                .first()
            )

            if existing_oauth:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User already exists",
                )

            # CRITICAL: Validate slug BEFORE creating user to avoid inconsistent state
            if request.tenant_slug:
                from nexus.server.auth.tenant_helpers import validate_tenant_id

                # Validate custom slug format
                is_valid, error_message = validate_tenant_id(request.tenant_slug)
                if not is_valid:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid tenant slug: {error_message}",
                    )

                # Check if slug already exists
                existing_tenant = session.get(TenantModel, request.tenant_slug)
                if existing_tenant:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Tenant slug '{request.tenant_slug}' is already taken. Please choose a different slug.",
                    )

        # Create user via OAuth system
        # Reconstruct OAuth credential
        from nexus.server.auth.oauth_provider import OAuthCredential

        oauth_credential = OAuthCredential(
            access_token=oauth_tokens["access_token"],
            refresh_token=oauth_tokens["refresh_token"],
            token_type=oauth_tokens.get("token_type", "Bearer"),
            expires_at=datetime.fromisoformat(oauth_tokens["expires_at"]) if oauth_tokens["expires_at"] else None,
        )

        # Create user using the OAuth helper
        with auth.session_factory() as session:
            user, _is_new = await oauth._get_or_create_oauth_user(
                session=session,
                provider="google",
                provider_user_id=provider_user_id,
                provider_email=provider_email,
                email_verified=True,
                name=name,
                picture=picture,
                oauth_credential=oauth_credential,
            )

        # Create tenant and API key (same logic as regular OAuth callback)
        user_role = "admin" if is_personal else "member"
        tenant_id = None
        api_key = None

        with auth.session_factory() as session:
            # Use custom slug if provided (already validated above), otherwise use suggested slug
            if request.tenant_slug:
                tenant_id = request.tenant_slug
            else:
                # Normalize slug and find available tenant_id
                normalized_slug = normalize_to_slug(base_slug)
                tenant_id = suggest_tenant_id(normalized_slug, session)

            # Build tenant description
            description = (
                f"Personal organization for {name or provider_email}"
                if is_personal
                else f"Organization for {email_domain}"
            )

            # Create or get existing tenant
            existing_tenant = session.get(TenantModel, tenant_id)
            if not existing_tenant:
                try:
                    create_tenant(
                        session=session,
                        tenant_id=tenant_id,
                        name=tenant_name,
                        domain=email_domain,
                        description=description,
                    )
                    tenant_type = "personal" if is_personal else "company"
                    logger.info(
                        f"Created {tenant_type} tenant '{tenant_id}' ('{tenant_name}') for user {provider_email}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to create tenant metadata for {tenant_id}: {e}")
            else:
                logger.info(f"User {provider_email} joining existing tenant '{tenant_id}' ('{existing_tenant.name}')")

        # Register user in entity registry
        entity_registry = EntityRegistry(auth.session_factory)
        try:
            entity_registry.register_entity(
                entity_type="user",
                entity_id=user.user_id,
                parent_type="tenant",
                parent_id=tenant_id,
            )
        except Exception as e:
            logger.debug(f"Entity registration skipped (may already exist): {e}")

        # Add user to tenant via ReBAC
        try:
            rebac_manager = auth.rebac_manager
            add_user_to_tenant(
                rebac_manager=rebac_manager,
                user_id=user.user_id,
                tenant_id=tenant_id,
                role=user_role,
            )
            logger.info(f"Added user {provider_email} to tenant '{tenant_id}' as {user_role}")
        except Exception as e:
            logger.warning(f"Failed to add user to tenant via ReBAC: {e}")

        # Create API key
        with auth.session_factory() as session:
            user_model = session.get(UserModel, user.user_id)
            if user_model and user_model.api_key:
                api_key = user_model.api_key
            else:
                key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                    session=session,
                    user_id=user.user_id,
                    name="Personal API Key",
                    subject_type="user",
                    subject_id=user.user_id,
                    tenant_id=tenant_id,
                    is_admin=False,
                    expires_at=None,
                    inherit_permissions=True,
                )

                if user_model:
                    user_model.api_key = raw_key
                    user_model.tenant_id = tenant_id

                session.commit()
                api_key = raw_key

        # Generate JWT token
        user_info_dict = auth.get_user_info_for_jwt(user.user_id)
        if not user_info_dict:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve user information",
            )
        token = auth.create_token(user.email or provider_email, user_info_dict)

        # Get complete user info
        user_info = auth.get_user_info(user.user_id) or {}

        logger.info(f"User registration confirmed: {provider_email} (tenant: {tenant_id})")

        return OAuthCallbackResponse(
            token=token,
            user=user_info,
            is_new_user=True,
            api_key=api_key,
            tenant_id=tenant_id,
            message="Registration completed successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth confirmation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration confirmation failed: {str(e)}",
        )


@router.post("/oauth/callback", response_model=OAuthCallbackResponse)
async def handle_oauth_callback(
    request: OAuthCallbackRequest,
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> OAuthCallbackResponse:
    """Handle OAuth callback.

    Exchanges authorization code for tokens, creates/links user account,
    and returns JWT token for Nexus access.

    Note: State validation should be done by the client before calling this endpoint.
    """
    if request.provider != "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {request.provider}",
        )

    try:
        # Handle Google OAuth callback
        user, token = await oauth.handle_google_callback(request.code, request.state)

        # Check if this is a new user (created in this request)
        is_new_user = user.created_at and (
            datetime.utcnow() - user.created_at
        ).total_seconds() < 5

        # Get user info
        user_info = auth.get_user_info(user.user_id)
        if not user_info:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve user info",
            )

        # Self-serve: Auto-create API key for users who don't have one yet
        api_key = None
        tenant_id = None

        # Check if user already has an API key
        existing_api_key = user_info.get('api_key')
        existing_tenant_id = user_info.get('tenant_id')

        # Create API key if user doesn't have one (for both new and existing users)
        if not existing_api_key and user.email:
            from nexus.core.entity_registry import EntityRegistry
            from nexus.server.auth.database_key import DatabaseAPIKeyAuth
            from nexus.server.auth.tenant_helpers import (
                create_tenant,
                get_tenant_strategy_from_email,
                normalize_to_slug,
                suggest_tenant_id,
            )
            from nexus.storage.models import TenantModel, UserModel

            # Determine tenant strategy based on email domain
            # Personal emails (gmail.com) → personal workspace
            # Company emails (xxx@acme.com) → company tenant
            (
                base_slug,
                tenant_name_base,
                email_domain,
                is_personal,
            ) = get_tenant_strategy_from_email(user.email)

            # Determine user role based on tenant type (defined here for use later)
            if is_personal:
                user_role = "admin"  # User is admin of their own workspace
            else:
                user_role = "member"  # Regular member of company

            with auth.session_factory() as session:
                # Normalize base slug and find available tenant_id
                normalized_slug = normalize_to_slug(base_slug)
                tenant_id = suggest_tenant_id(normalized_slug, session)

                # Determine tenant name based on strategy
                if is_personal:
                    # Personal workspace: "Alice's Workspace"
                    first_name = None
                    if user.display_name:
                        first_name = user.display_name.split()[0].strip()
                    tenant_name = f"{first_name or tenant_name_base}'s Workspace"
                    description = f"Personal workspace for {user.display_name or user.email}"
                else:
                    # Company tenant: "Acme" (from domain)
                    tenant_name = tenant_name_base
                    description = f"Organization workspace for {email_domain}"

                # Create or get existing tenant
                existing_tenant = session.get(TenantModel, tenant_id)
                if not existing_tenant:
                    try:
                        tenant = create_tenant(
                            session=session,
                            tenant_id=tenant_id,
                            name=tenant_name,
                            domain=email_domain,
                            description=description,
                        )
                        tenant_type = "personal" if is_personal else "company"
                        logger.info(
                            f"Created {tenant_type} tenant '{tenant_id}' ('{tenant_name}') "
                            f"for user {user.email}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to create tenant metadata for {tenant_id}: {e}. "
                            "Continuing with tenant_id only."
                        )
                        # Continue even if tenant creation fails
                        pass
                else:
                    # Tenant already exists (e.g., another user from same company)
                    logger.info(
                        f"User {user.email} joining existing tenant '{tenant_id}' ('{existing_tenant.name}')"
                    )

            # Register user in entity registry (if not already registered)
            entity_registry = EntityRegistry(auth.session_factory)
            try:
                entity_registry.register_entity(
                    entity_type="user",
                    entity_id=user.user_id,
                    parent_type="tenant",
                    parent_id=tenant_id,
                )
            except Exception as e:
                # Entity might already be registered, which is fine
                logger.debug(f"Entity registration skipped (may already exist): {e}")

            # Add user to tenant via ReBAC (if not already a member)
            try:
                from nexus.server.auth.user_helpers import add_user_to_tenant

                # Get rebac_manager from auth module
                rebac_manager = auth.rebac_manager
                add_user_to_tenant(
                    rebac_manager=rebac_manager,
                    user_id=user.user_id,
                    tenant_id=tenant_id,
                    role=user_role,  # "admin" for personal workspace, "member" for company
                )
                logger.info(f"Added user {user.email} to tenant '{tenant_id}' as {user_role}")
            except Exception as e:
                logger.warning(
                    f"Failed to add user to tenant via ReBAC: {e}. "
                    "User may need to be added manually."
                )

            # Create API key for the user (with race condition protection)
            with auth.session_factory() as session:
                # Double-check if API key was created by concurrent request
                user_model = session.get(UserModel, user.user_id)
                if user_model and user_model.api_key:
                    # Another request already created the API key
                    api_key = user_model.api_key
                    logger.debug(f"API key already exists for user {user.email}, using existing key")
                else:
                    # Create new API key
                    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                        session=session,
                        user_id=user.user_id,
                        name="Personal API Key",
                        subject_type="user",
                        subject_id=user.user_id,
                        tenant_id=tenant_id,
                        is_admin=False,  # Regular users are not admins
                        expires_at=None,  # No expiry for personal keys
                        inherit_permissions=True,  # User has their own permissions
                    )

                    # Store plaintext API key in users table for retrieval
                    if user_model:
                        user_model.api_key = raw_key
                        user_model.tenant_id = tenant_id

                    session.commit()
                    api_key = raw_key

            if api_key:
                user_type = "new" if is_new_user else "existing"
                logger.info(f"API key ready for {user_type} user: {user.email} (tenant: {tenant_id})")

            # Refresh user_info to include the newly created API key
            user_info = auth.get_user_info(user.user_id)

        # If API key wasn't just created, extract from user_info (already exists in database)
        if api_key is None and user_info.get('api_key'):
            api_key = user_info['api_key']
        if tenant_id is None and user_info.get('tenant_id'):
            tenant_id = user_info['tenant_id']

        return OAuthCallbackResponse(
            token=token,
            user=user_info,
            is_new_user=is_new_user,
            api_key=api_key,
            tenant_id=tenant_id,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth authentication failed: {e}",
        )


@router.get("/my-api-keys")
async def list_my_api_keys(
    current_user: AuthResult = Depends(get_current_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> dict[str, Any]:
    """List API keys for the current authenticated user.

    Self-serve endpoint for users to view their own API keys.
    """
    from nexus.storage.models import APIKeyModel
    from sqlalchemy import select

    with auth.session_factory() as session:
        # Query API keys for this user
        stmt = select(APIKeyModel).where(
            APIKeyModel.user_id == current_user.subject_id,
            APIKeyModel.revoked == 0,  # SQLite boolean
        ).order_by(APIKeyModel.created_at.desc())

        api_keys = session.scalars(stmt).all()

        # Format response (exclude key_hash for security)
        keys_data = []
        for key in api_keys:
            keys_data.append({
                "key_id": key.key_id,
                "name": key.name,
                "tenant_id": key.tenant_id,
                "subject_type": key.subject_type,
                "is_admin": bool(key.is_admin),
                "created_at": key.created_at.isoformat() if key.created_at else None,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            })

        return {
            "keys": keys_data,
            "count": len(keys_data),
        }


@router.get("/oauth/accounts", response_model=list[OAuthAccountInfo])
async def list_oauth_accounts(
    current_user: AuthResult = Depends(get_current_user),
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
) -> list[OAuthAccountInfo]:
    """List OAuth accounts linked to current user."""
    accounts = oauth.get_user_oauth_accounts(current_user.subject_id)

    return [OAuthAccountInfo(**account) for account in accounts]


@router.delete("/oauth/accounts/{oauth_account_id}", response_model=MessageResponse)
async def unlink_oauth_account(
    oauth_account_id: str,
    current_user: AuthResult = Depends(get_current_user),
    oauth: OAuthUserAuth = Depends(get_oauth_provider),
) -> MessageResponse:
    """Unlink OAuth account from current user."""
    try:
        oauth.unlink_oauth_account(current_user.subject_id, oauth_account_id)

        return MessageResponse(
            message="OAuth account unlinked successfully",
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# ==============================================================================
# Email Verification Endpoints (TODO)
# ==============================================================================


class VerifyEmailRequest(BaseModel):
    """Email verification request."""

    token: str = Field(..., description="Email verification token")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    request: VerifyEmailRequest,
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> MessageResponse:
    """Verify email with verification token.

    TODO: Implement email verification token validation.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Email verification not yet implemented",
    )


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification_email(
    current_user: AuthResult = Depends(get_current_user),
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> MessageResponse:
    """Resend verification email.

    TODO: Implement verification email resending.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Email verification not yet implemented",
    )


# ==============================================================================
# Password Reset Endpoints (TODO)
# ==============================================================================


class ResetPasswordRequest(BaseModel):
    """Password reset request."""

    email: EmailStr = Field(..., description="User email address")


@router.post("/reset-password", response_model=MessageResponse)
async def request_password_reset(
    request: ResetPasswordRequest,
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> MessageResponse:
    """Request password reset via email.

    TODO: Implement password reset email sending.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Password reset not yet implemented",
    )


class ResetPasswordConfirmRequest(BaseModel):
    """Password reset confirmation request."""

    token: str = Field(..., description="Password reset token")
    new_password: str = Field(..., min_length=12, description="New password")


@router.post("/reset-password/confirm", response_model=MessageResponse)
async def confirm_password_reset(
    request: ResetPasswordConfirmRequest,
    auth: DatabaseLocalAuth = Depends(get_auth_provider),
) -> MessageResponse:
    """Confirm password reset with token.

    TODO: Implement password reset token validation.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Password reset not yet implemented",
    )
