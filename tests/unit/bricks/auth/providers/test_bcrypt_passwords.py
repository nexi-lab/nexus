from __future__ import annotations

import bcrypt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
from nexus.bricks.auth.providers.local import LocalAuth
from nexus.storage.models import Base

LONG_PASSWORD = "p" * 80


def test_local_auth_verifies_existing_bcrypt_hash_for_long_password() -> None:
    password_hash = bcrypt.hashpw(LONG_PASSWORD.encode("utf-8")[:72], bcrypt.gensalt()).decode(
        "utf-8"
    )
    auth = LocalAuth(
        jwt_secret="test-secret",
        users={
            "long@example.com": {
                "password_hash": password_hash,
                "subject_type": "user",
                "subject_id": "long",
            }
        },
    )

    assert auth.verify_password("long@example.com", LONG_PASSWORD) is not None


def test_local_auth_can_create_user_with_long_password() -> None:
    auth = LocalAuth(jwt_secret="test-secret")

    auth.create_user("long@example.com", LONG_PASSWORD)

    assert auth.verify_password("long@example.com", LONG_PASSWORD) is not None


@pytest.fixture
def database_auth() -> DatabaseLocalAuth:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return DatabaseLocalAuth(
        session_factory=session_factory,
        jwt_secret="test-secret",
        token_expiry=3600,
    )


def test_database_auth_can_register_and_login_with_long_password(
    database_auth: DatabaseLocalAuth,
) -> None:
    user = database_auth.register_user(
        email="long@example.com",
        password=LONG_PASSWORD,
        username="long",
    )
    token = database_auth.create_email_verification_token(user.user_id, user.email)
    database_auth.verify_email(user.user_id, token)

    assert database_auth.login("long@example.com", LONG_PASSWORD) is not None


def test_database_auth_can_change_to_long_password(database_auth: DatabaseLocalAuth) -> None:
    user = database_auth.register_user(
        email="change@example.com",
        password="short-password",
        username="change",
    )
    token = database_auth.create_email_verification_token(user.user_id, user.email)
    database_auth.verify_email(user.user_id, token)

    assert database_auth.change_password(user.user_id, "short-password", LONG_PASSWORD) is True
    assert database_auth.login("change@example.com", LONG_PASSWORD) is not None
