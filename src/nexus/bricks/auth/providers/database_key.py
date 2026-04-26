"""Database-backed API key authentication provider."""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.bricks.auth.constants import (
    _HMAC_SALT_DEFAULT,
    API_KEY_MIN_LENGTH,
    API_KEY_PREFIX,
    get_hmac_secret,
)
from nexus.bricks.auth.providers.base import AuthProvider, AuthResult

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class DatabaseAPIKeyAuth(AuthProvider):
    """Database-backed API key authentication with expiry and revocation.

    P0-5 Security features:
    - API keys stored with HMAC-SHA256 + salt
    - Mandatory sk- prefix validation
    - Mandatory expiry for production keys
    - Revocation support with cache invalidation
    - Audit trail of key usage
    """

    def __init__(self, record_store: "RecordStoreABC", require_expiry: bool = False) -> None:
        self._record_store = record_store
        self.session_factory = record_store.session_factory
        self.require_expiry = require_expiry
        logger.info("Initialized DatabaseAPIKeyAuth (require_expiry=%s)", require_expiry)

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using database API key."""
        from nexus.storage.models import APIKeyModel, ZoneModel

        if not token:
            return AuthResult(authenticated=False)

        if not self._validate_key_format(token):
            logger.warning(
                "UNAUTHORIZED: Invalid API key format (must start with %s)", API_KEY_PREFIX
            )
            return AuthResult(authenticated=False)

        token_hash = self._hash_key(token)

        with self.session_factory() as session:
            stmt = select(APIKeyModel).where(
                APIKeyModel.key_hash == token_hash,
                APIKeyModel.revoked == 0,
            )
            api_key = session.scalar(stmt)

            # Issue #3062: Dual-read fallback — if a custom HMAC secret is
            # configured but this key was minted with the legacy salt, retry
            # with the legacy salt so existing keys keep working during the
            # migration window.
            if not api_key and get_hmac_secret() != _HMAC_SALT_DEFAULT:
                legacy_hash = self._hash_key_with(token, _HMAC_SALT_DEFAULT)
                if legacy_hash != token_hash:
                    stmt_legacy = select(APIKeyModel).where(
                        APIKeyModel.key_hash == legacy_hash,
                        APIKeyModel.revoked == 0,
                    )
                    api_key = session.scalar(stmt_legacy)
                    if api_key:
                        token_hash = legacy_hash  # for background update

            if not api_key:
                logger.debug("API key not found or revoked: %s...", token_hash[:16])
                return AuthResult(authenticated=False)

            now = datetime.now(UTC)
            if api_key.expires_at:
                expires_at = api_key.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if now > expires_at:
                    logger.debug("UNAUTHORIZED: API key expired: %s", api_key.key_id)
                    return AuthResult(authenticated=False)
            elif self.require_expiry:
                logger.error(
                    "UNAUTHORIZED: API key %s has no expiry date. "
                    "Set require_expiry=False to allow keys without expiry.",
                    api_key.key_id,
                )
                return AuthResult(authenticated=False)

            # #3785: load token's zone allow-list (with per-zone perms, F3c)
            # from api_key_zones junction. Authoritative source post-#3871.
            from nexus.storage.models import APIKeyZoneModel

            zone_perm_rows = session.execute(
                select(APIKeyZoneModel.zone_id, APIKeyZoneModel.permissions)
                .where(APIKeyZoneModel.key_id == api_key.key_id)
                .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
            ).all()

            # #3871 round 2: legacy zone-scoped key without junction row
            # MUST fail closed. The pre-Task-6 SQLAlchemyAPIKeyStore.create_key
            # wrote api_key.zone_id without a junction row; under the new
            # junction-only auth path, an admin row would be silently
            # reinterpreted as a global/zoneless admin (privilege escalation).
            # The tripwire migration (04188c0bbb28) blocks the upgrade until
            # such rows are backfilled, but defense-in-depth here ensures we
            # never honor an unmigrated legacy row at auth time.
            if not zone_perm_rows and api_key.zone_id is not None:
                logger.warning(
                    "UNAUTHORIZED: API key %s has legacy zone_id=%r but no junction row "
                    "(pre-#3871 unmigrated key); refusing to authenticate",
                    api_key.key_id,
                    api_key.zone_id,
                )
                return AuthResult(authenticated=False)

            # #3784 round 10: zone lifecycle gate at runtime. A token scoped
            # to a zone that has since been soft-deleted or marked Terminating
            # must fail closed. Check EVERY junction zone (a token multi-zoned
            # to [eng, ops] must reject if EITHER becomes inactive — fail
            # closed semantics). Falls through if junction is empty (zoneless
            # admin keys) or if a zone is absent from the registry (legacy
            # back-compat per the original #3784 reasoning).
            for zid in [z for z, _ in zone_perm_rows]:
                zone = session.scalar(select(ZoneModel).where(ZoneModel.zone_id == zid))
                if zone is not None and (zone.phase != "Active" or zone.deleted_at is not None):
                    logger.warning(
                        "UNAUTHORIZED: API key %s zone %r is not active (phase=%s, deleted_at=%s)",
                        api_key.key_id,
                        zid,
                        zone.phase,
                        zone.deleted_at,
                    )
                    return AuthResult(authenticated=False)

            # Cache all ORM attributes eagerly before session close
            subject_type = (
                api_key.subject_type
                if hasattr(api_key, "subject_type") and api_key.subject_type
                else "user"
            )
            subject_id = (
                api_key.subject_id
                if hasattr(api_key, "subject_id") and api_key.subject_id
                else api_key.user_id
            )
            # After #3871 Phase 2: junction is the sole source of zone access.
            # api_key.zone_id (legacy column) is NOT consulted — empty junction
            # means no zone access. The tripwire migration (04188c0bbb28)
            # prevents Phase 3 upgrade until pre-Phase-2 keys are backfilled
            # to the junction.
            zone_id = zone_perm_rows[0][0] if zone_perm_rows else None
            is_admin = bool(api_key.is_admin)
            key_id = api_key.key_id
            key_name = api_key.name
            expires_at_iso = api_key.expires_at.isoformat() if api_key.expires_at else None

            # Derive zone_perms: junction is sole source of truth (#3871 Phase 2).
            # zone_set is rebuilt by AuthResult.__post_init__.
            zone_perms: tuple[tuple[str, str], ...] = tuple((z, p) for z, p in zone_perm_rows)

        # Decision #13: Fire-and-forget last_used_at update (outside session)
        self._update_last_used_background(token_hash)

        return AuthResult(
            authenticated=True,
            subject_type=subject_type,
            subject_id=subject_id,
            zone_id=zone_id,
            is_admin=is_admin,
            zone_perms=zone_perms,
            metadata={
                "key_id": key_id,
                "key_name": key_name,
                "expires_at": expires_at_iso,
            },
        )

    def _update_last_used_background(self, token_hash: str) -> None:
        """Fire-and-forget update of last_used_at (Decision #13).

        Runs in a separate session so it doesn't block the auth response.
        Failures are logged at WARNING but don't affect authentication.
        Uses a single UPDATE statement (no SELECT round-trip needed).
        """
        from sqlalchemy import update
        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            from nexus.storage.models import APIKeyModel

            with self.session_factory() as session:
                session.execute(
                    update(APIKeyModel)
                    .where(APIKeyModel.key_hash == token_hash)
                    .values(last_used_at=datetime.now(UTC))
                )
                session.commit()
        except (OperationalError, ProgrammingError):
            logger.warning("Failed to update last_used_at (non-critical)", exc_info=True)

    async def validate_token(self, token: str) -> bool:
        result = await self.authenticate(token)
        return result.authenticated

    def close(self) -> None:
        pass

    @staticmethod
    def _validate_key_format(key: str) -> bool:
        if not key.startswith(API_KEY_PREFIX):
            return False
        return len(key) >= API_KEY_MIN_LENGTH

    @staticmethod
    def _hash_key(key: str) -> str:
        secret = get_hmac_secret()
        return hmac.new(secret.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _hash_key_with(key: str, secret: str) -> str:
        """Hash a key with a specific HMAC secret (used for legacy fallback)."""
        return hmac.new(secret.encode("utf-8"), key.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def create_key(
        cls,
        session: Session,
        user_id: str,
        name: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,
    ) -> tuple[str, str]:
        """Create a new API key in the database.

        Returns:
            Tuple of (key_id, raw_key). Raw key is only returned once.
        """
        from nexus.storage.models import APIKeyModel

        final_subject_id = subject_id or user_id
        valid_subject_types = ["user", "agent", "service"]
        if subject_type not in valid_subject_types:
            raise ValueError(
                f"subject_type must be one of {valid_subject_types}, got {subject_type}"
            )

        zone_prefix = f"{zone_id[:8]}_" if zone_id else ""
        subject_prefix = final_subject_id[:12] if subject_type == "agent" else user_id[:8]
        random_suffix = secrets.token_hex(16)
        key_id_part = secrets.token_hex(4)

        raw_key = f"{API_KEY_PREFIX}{zone_prefix}{subject_prefix}_{key_id_part}_{random_suffix}"
        key_hash = cls._hash_key(raw_key)

        api_key = APIKeyModel(
            key_hash=key_hash,
            user_id=user_id,
            name=name,
            zone_id=None,  # #3871 Phase 2: junction is source of truth
            is_admin=int(is_admin),
            expires_at=expires_at,
            subject_type=subject_type,
            subject_id=final_subject_id,
            inherit_permissions=int(inherit_permissions),
        )

        session.add(api_key)
        session.flush()  # populate api_key.key_id before junction insert

        if zone_id:  # populate junction so the key is visible to junction-based filters
            from nexus.storage.models import APIKeyZoneModel

            session.add(APIKeyZoneModel(key_id=api_key.key_id, zone_id=zone_id, permissions="rw"))
            session.flush()

        return (api_key.key_id, raw_key)

    @classmethod
    def revoke_key(cls, session: Session, key_id: str, *, zone_id: str | None = None) -> bool:
        """Revoke an API key.

        Args:
            session: SQLAlchemy session.
            key_id: Key ID to revoke.
            zone_id: Zone access filter. When provided, only keys that
                grant access to this zone (via the api_key_zones junction)
                can be revoked. Multi-zone keys match on every granted
                zone, not only the primary (#3871).

        Returns:
            True if key was revoked, False if not found.
        """
        from nexus.storage.models import APIKeyModel

        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        if zone_id is not None:
            from nexus.storage.models import APIKeyZoneModel

            stmt = stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id).where(
                APIKeyZoneModel.zone_id == zone_id
            )
        api_key = session.scalar(stmt)

        if not api_key:
            return False

        api_key.revoked = 1
        api_key.revoked_at = datetime.now(UTC)
        session.flush()

        return True
