#!/usr/bin/env python3
"""
Create an API key for a test agent.

Usage:
    python create_test_agent_key.py <database_url> <api_key> <agent_id> <user_id> <tenant_id> <agent_name>
"""

import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel


def create_test_agent_key(
    database_url: str,
    api_key: str,
    agent_id: str,
    user_id: str,
    tenant_id: str,
    agent_name: str,
) -> bool:
    """Create or update an API key for a test agent.

    Args:
        database_url: PostgreSQL connection string
        api_key: The API key string to hash and store
        agent_id: Full agent ID (e.g., "admin,TestAgent")
        user_id: User ID (e.g., "admin")
        tenant_id: Tenant ID (e.g., "default")
        agent_name: Human-readable agent name

    Returns:
        True if successful, False otherwise
    """
    try:
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)

        # Create API key
        with SessionFactory() as session:
            key_hash = DatabaseAPIKeyAuth._hash_key(api_key)

            # Check if key already exists for this agent
            existing = session.execute(
                select(APIKeyModel).where(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == agent_id,
                    APIKeyModel.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()

            if existing:
                print(f"Updating existing API key for {agent_id}", file=sys.stderr)
                existing.key_hash = key_hash
            else:
                print(f"Creating new API key for {agent_id}", file=sys.stderr)
                api_key_model = APIKeyModel(
                    key_hash=key_hash,
                    user_id=user_id,
                    subject_type="agent",
                    subject_id=agent_id,
                    tenant_id=tenant_id,
                    is_admin=0,
                    name=f"{agent_name} Test Key",
                    created_at=datetime.now(UTC),
                    expires_at=None,
                    revoked=0,
                    revoked_at=None,
                    last_used_at=None,
                    inherit_permissions=0,
                )
                session.add(api_key_model)

            session.commit()
            print(f"✓ API key created/updated for {agent_id}", file=sys.stderr)
            return True

    except Exception as e:
        print(f"✗ Error creating API key: {e}", file=sys.stderr)
        return False


def main() -> int:
    """Main entry point."""
    if len(sys.argv) != 7:
        print(
            "Usage: create_test_agent_key.py <database_url> <api_key> <agent_id> <user_id> <tenant_id> <agent_name>"
        )
        return 1

    database_url = sys.argv[1]
    api_key = sys.argv[2]
    agent_id = sys.argv[3]
    user_id = sys.argv[4]
    tenant_id = sys.argv[5]
    agent_name = sys.argv[6]

    success = create_test_agent_key(
        database_url=database_url,
        api_key=api_key,
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=tenant_id,
        agent_name=agent_name,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
