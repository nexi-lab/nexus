"""SQLAlchemy implementation of OAuthCredentialStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import OAuthCredentialDTO
from nexus.storage.models import OAuthCredentialModel

logger = logging.getLogger(__name__)


def _to_dto(model: OAuthCredentialModel) -> OAuthCredentialDTO:
    return OAuthCredentialDTO(
        credential_id=model.credential_id,
        provider=model.provider,
        user_email=model.user_email,
        zone_id=model.zone_id,
        user_id=model.user_id,
        token_type=model.token_type,
        expires_at=model.expires_at,
        revoked=bool(model.revoked),
        scopes=model.scopes,
        last_used_at=model.last_used_at,
        last_refreshed_at=model.last_refreshed_at,
        created_at=model.created_at,
        token_family_id=model.token_family_id,
        rotation_counter=model.rotation_counter or 0,
    )


class SQLAlchemyOAuthCredentialStore:
    """OAuthCredentialStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def store_credential(
        self,
        *,
        provider: str,
        user_email: str,
        zone_id: str,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None = None,
        token_type: str = "Bearer",
        expires_at: datetime | None = None,
        scopes: str | None = None,
        client_id: str | None = None,
        token_uri: str | None = None,
        user_id: str | None = None,
        created_by: str | None = None,
        refresh_token_hash: str | None = None,
    ) -> OAuthCredentialDTO:
        with self._session_factory() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == provider,
                OAuthCredentialModel.user_email == user_email,
                OAuthCredentialModel.zone_id == zone_id,
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                existing.encrypted_access_token = encrypted_access_token
                # Preserve existing refresh token if new one is not provided
                if encrypted_refresh_token is not None:
                    existing.encrypted_refresh_token = encrypted_refresh_token
                existing.token_type = token_type
                existing.expires_at = expires_at
                existing.scopes = scopes
                existing.client_id = client_id
                existing.token_uri = token_uri
                existing.user_id = user_id
                existing.updated_at = datetime.now(UTC)
                existing.revoked = 0
                existing.token_family_id = str(uuid.uuid4())
                existing.rotation_counter = 0
                existing.refresh_token_hash = refresh_token_hash
                session.commit()
                return _to_dto(existing)

            model = OAuthCredentialModel(
                provider=provider,
                user_email=user_email,
                user_id=user_id,
                zone_id=zone_id,
                encrypted_access_token=encrypted_access_token,
                encrypted_refresh_token=encrypted_refresh_token,
                token_type=token_type,
                expires_at=expires_at,
                scopes=scopes,
                client_id=client_id,
                token_uri=token_uri,
                created_by=created_by,
                token_family_id=str(uuid.uuid4()),
                rotation_counter=0,
                refresh_token_hash=refresh_token_hash,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_dto(model)

    def get_credential(
        self, provider: str, user_email: str, zone_id: str
    ) -> OAuthCredentialDTO | None:
        with self._session_factory() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.provider == provider,
                    OAuthCredentialModel.user_email == user_email,
                    OAuthCredentialModel.zone_id == zone_id,
                    OAuthCredentialModel.revoked == 0,
                )
            ).scalar_one_or_none()
            return _to_dto(model) if model else None

    def update_tokens(
        self,
        credential_id: str,
        *,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None = None,
        expires_at: datetime | None = None,
        refresh_token_hash: str | None = None,
        rotation_counter: int | None = None,
        token_family_id: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            model = session.get(OAuthCredentialModel, credential_id)
            if not model:
                return
            model.encrypted_access_token = encrypted_access_token
            if encrypted_refresh_token is not None:
                model.encrypted_refresh_token = encrypted_refresh_token
            if expires_at is not None:
                model.expires_at = expires_at
            if refresh_token_hash is not None:
                model.refresh_token_hash = refresh_token_hash
            if rotation_counter is not None:
                model.rotation_counter = rotation_counter
            if token_family_id is not None:
                model.token_family_id = token_family_id
            model.last_refreshed_at = datetime.now(UTC)
            model.updated_at = datetime.now(UTC)
            session.commit()

    def revoke_credential(self, provider: str, user_email: str, zone_id: str) -> bool:
        with self._session_factory() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.provider == provider,
                    OAuthCredentialModel.user_email == user_email,
                    OAuthCredentialModel.zone_id == zone_id,
                )
            ).scalar_one_or_none()
            if not model:
                return False
            model.revoked = 1
            model.revoked_at = datetime.now(UTC)
            session.commit()
            return True

    def list_credentials(
        self,
        *,
        zone_id: str | None = None,
        user_email: str | None = None,
        user_id: str | None = None,
    ) -> list[OAuthCredentialDTO]:
        with self._session_factory() as session:
            stmt = select(OAuthCredentialModel).where(OAuthCredentialModel.revoked == 0)
            if zone_id is not None:
                stmt = stmt.where(OAuthCredentialModel.zone_id == zone_id)
            if user_id is not None:
                stmt = stmt.where(OAuthCredentialModel.user_id == user_id)
            elif user_email is not None:
                stmt = stmt.where(OAuthCredentialModel.user_email == user_email)
            models = session.execute(stmt).scalars().all()
            return [_to_dto(m) for m in models]
