"""SQLAlchemy implementation of OAuthAccountStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import OAuthAccountDTO
from nexus.storage.models import UserOAuthAccountModel


def _to_dto(account: UserOAuthAccountModel) -> OAuthAccountDTO:
    return OAuthAccountDTO(
        id=account.oauth_account_id,
        user_id=account.user_id,
        provider=account.provider,
        provider_user_id=account.provider_user_id,
        provider_email=account.provider_email,
        display_name=None,  # Not stored on UserOAuthAccountModel
        last_used_at=account.last_used_at,
        created_at=account.created_at,
    )


class SQLAlchemyOAuthAccountStore:
    """OAuthAccountStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create_account(
        self,
        *,
        user_id: str,
        provider: str,
        provider_user_id: str,
        provider_email: str | None = None,
        display_name: str | None = None,
    ) -> OAuthAccountDTO:
        _ = display_name  # Protocol conformance; not stored on ORM model
        account = UserOAuthAccountModel(
            oauth_account_id=str(uuid.uuid4()),
            user_id=user_id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=provider_email,
            created_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )
        with self._session_factory() as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return _to_dto(account)

    def get_by_provider(self, provider: str, provider_user_id: str) -> OAuthAccountDTO | None:
        with self._session_factory() as session:
            account = session.scalar(
                select(UserOAuthAccountModel).where(
                    UserOAuthAccountModel.provider == provider,
                    UserOAuthAccountModel.provider_user_id == provider_user_id,
                )
            )
            return _to_dto(account) if account else None

    def get_accounts_for_user(self, user_id: str) -> list[OAuthAccountDTO]:
        with self._session_factory() as session:
            accounts = (
                session.execute(
                    select(UserOAuthAccountModel).where(UserOAuthAccountModel.user_id == user_id)
                )
                .scalars()
                .all()
            )
            return [_to_dto(a) for a in accounts]

    def update_last_used(self, oauth_account_id: str) -> None:
        with self._session_factory() as session:
            account = session.get(UserOAuthAccountModel, oauth_account_id)
            if account:
                account.last_used_at = datetime.now(UTC)
                session.commit()

    def delete_account(self, oauth_account_id: str) -> bool:
        with self._session_factory() as session:
            account = session.get(UserOAuthAccountModel, oauth_account_id)
            if not account:
                return False
            session.delete(account)
            session.commit()
            return True
