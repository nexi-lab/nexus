"""Authentication providers for Nexus server.

Server-layer auth modules:
- auth_routes: FastAPI routes for authentication (register, login, OAuth callbacks)
- zone_routes: FastAPI routes for zone management
- factory: Auth provider factory (create_auth_provider)
- oauth_init: OAuth provider initialization
- oauth_provider: OAuthCredential, OAuthProvider, OAuthError interfaces
- email_sender: Email verification sender
"""
