"""OAuth CSRF state service — stateless, signed, browser-bound.

Implements RFC 6749 §10.12 plus the OAuth 2.0 Security BCP recommendation to
bind ``state`` to the user-agent that started the flow. Each authorize
redirect generates:

* ``binding_nonce`` — random 32-byte URL-safe string. Never leaves the
  origin; set in an HttpOnly, SameSite=Lax cookie on the browser.
* ``state`` — a signed, TTL-bounded token (``itsdangerous`` URL-safe
  serializer) whose payload carries the ``binding_nonce``. Embedded in
  the Google redirect URL.

On callback the server verifies the signature, enforces the TTL, and
re-checks that the ``binding_nonce`` inside the signed state matches the
cookie. A state issued for one browser is useless in another — which
blocks OAuth login-fixation / account-takeover. No server-side storage,
so this is safe under any number of workers or replicas.

Single-use of ``state`` itself is NOT enforced here (doing so would
require shared storage again). Replay protection comes from the
authorization ``code``: Google marks it consumed on the first exchange,
so a second POST with the same ``(code, state)`` fails at
``exchange_code`` with ``invalid_grant`` regardless.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

# Salt namespaces the signer so the OAuth state tokens cannot be confused
# with any other ``itsdangerous``-signed artefact that happens to share
# the same secret.
_SIGNER_SALT = "nexus.oauth.state.v1"

DEFAULT_STATE_TTL_SECONDS = 600


class OAuthStateService:
    """Issues and verifies browser-bound, signed OAuth state tokens.

    Stateless: the state token carries its own binding + timestamp and is
    verified by HMAC signature with the server's JWT secret. Safe across
    multiple uvicorn workers and multiple server replicas — no sticky
    sessions required.
    """

    def __init__(self, signing_secret: str, ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS) -> None:
        if not signing_secret:
            raise ValueError("signing_secret must be a non-empty string")
        self._signer = URLSafeTimedSerializer(signing_secret, salt=_SIGNER_SALT)
        self._ttl = ttl_seconds

    def issue(self, binding_nonce: str) -> str:
        """Return a signed state value bound to ``binding_nonce``.

        The caller must set ``binding_nonce`` as an HttpOnly cookie on the
        initiating browser. The state value can be sent through Google's
        redirect unchanged; the cookie never leaves the origin.
        """
        if not binding_nonce:
            raise ValueError("binding_nonce must be a non-empty string")
        payload = {
            "n": secrets.token_urlsafe(16),  # opacity; unique per issue call
            "b": binding_nonce,
        }
        return self._signer.dumps(payload)

    def verify(self, state: str | None, binding_nonce: str | None) -> bool:
        """Verify state signature + TTL + binding matches the cookie nonce.

        Returns ``True`` only when all three hold. Constant-time comparison
        on the nonce. An expired, tampered, or cross-browser state returns
        ``False``.
        """
        if not state or not binding_nonce:
            return False
        try:
            payload = self._signer.loads(state, max_age=self._ttl)
        except SignatureExpired:
            logger.debug("OAuth state rejected: expired")
            return False
        except BadSignature:
            logger.debug("OAuth state rejected: bad signature")
            return False
        if not isinstance(payload, dict):
            return False
        stored = payload.get("b")
        if not isinstance(stored, str) or not stored:
            return False
        return hmac.compare_digest(stored, binding_nonce)


_state_service: OAuthStateService | None = None


def initialize_oauth_state_service(
    signing_secret: str, ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS
) -> OAuthStateService:
    """Initialize the process-wide OAuth state service.

    Called from the server bootstrap once a signing secret (the JWT secret)
    is available. Subsequent calls replace the instance — useful for tests.
    """
    global _state_service
    _state_service = OAuthStateService(signing_secret, ttl_seconds=ttl_seconds)
    return _state_service


def get_oauth_state_service() -> OAuthStateService:
    """Return the initialized OAuth state service.

    Raises:
        RuntimeError: If ``initialize_oauth_state_service`` has not been called.
    """
    if _state_service is None:
        raise RuntimeError(
            "OAuth state service not initialized — call "
            "initialize_oauth_state_service(jwt_secret) during server startup."
        )
    return _state_service
