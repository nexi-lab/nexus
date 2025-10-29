#!/usr/bin/env python3
"""Create agent API keys for Nexus.

v0.5.0: Simplified agent creation using auth-agnostic registration.

Most agents should use register_agent() without API keys.
Only create API keys if agent needs independent authentication.

Usage:
    # Register agent (no API key, uses user auth)
    python scripts/create-agent-key.py alice agent_data_analyst "Data Analyst"

    # Register agent WITH API key (for independent auth)
    python scripts/create-agent-key.py alice agent_service "Service" --with-key --days 90

v0.5.0: Part of ACE (Agentic Context Engineering) integration
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.agents import create_agent_with_api_key, register_agent
from nexus.core.entity_registry import EntityRegistry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register Nexus agent (v0.5.0 ACE - Auth-agnostic)",
        epilog="""
Examples:
  # Register agent (NO API key, uses user auth + X-Agent-ID header)
  python scripts/create-agent-key.py alice agent_analyst "Data Analyst"

  # Register agent WITH API key (for independent authentication)
  python scripts/create-agent-key.py alice agent_service "Service" --with-key --days 90

Agent Identity System (v0.5.0):
  - Most agents should NOT have API keys (use user's auth + X-Agent-ID header)
  - Only create API keys if agent needs independent authentication
  - Agents inherit permissions from their owner (user)
  - Agent lifecycle managed via API key TTL (if key exists)
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("user_id", help="User ID (owner) - e.g., alice, bob")
    parser.add_argument("agent_id", help="Agent ID - e.g., agent_data_analyst, agent_code_gen")
    parser.add_argument("name", help="Agent name (human-readable) - e.g., 'Data Analyst'")
    parser.add_argument(
        "--with-key", action="store_true", help="Create API key for agent (optional)"
    )
    parser.add_argument(
        "--admin", action="store_true", help="Grant admin privileges (requires --with-key)"
    )
    parser.add_argument("--days", type=int, help="API key expiry in days (requires --with-key)")
    parser.add_argument("--tenant-id", default="default", help="Tenant ID (default: default)")

    args = parser.parse_args()

    # Validate admin/days require --with-key
    if (args.admin or args.days) and not args.with_key:
        print("❌ Error: --admin and --days require --with-key flag")
        sys.exit(1)

    # Validate agent_id format (should start with 'agent_' by convention)
    if not args.agent_id.startswith("agent_"):
        print(
            f"⚠️  Warning: Agent ID '{args.agent_id}' doesn't follow convention (should start with 'agent_')"
        )
        response = input("Continue anyway? [y/N] ")
        if response.lower() != "y":
            sys.exit(0)

    # Get database URL from environment
    database_url = os.getenv("NEXUS_DATABASE_URL")
    if not database_url:
        print("❌ Error: NEXUS_DATABASE_URL environment variable not set")
        print("Example: export NEXUS_DATABASE_URL='postgresql://nexus:password@localhost/nexus'")
        sys.exit(1)

    # Create engine and session
    engine = create_engine(database_url)
    SessionFactory = sessionmaker(bind=engine)

    # Calculate expiry if specified
    expires_at = None
    if args.days:
        expires_at = datetime.now(UTC) + timedelta(days=args.days)

    # Create agent (with or without API key)
    try:
        # Setup entity registry (pass factory, not session!)
        registry = EntityRegistry(SessionFactory)

        if args.with_key:
            # Create agent WITH API key
            # Pass Session (not factory) to create_agent_with_api_key for transaction
            session = SessionFactory()
            try:
                agent, raw_key = create_agent_with_api_key(
                    session,
                    user_id=args.user_id,
                    agent_id=args.agent_id,
                    name=args.name,
                    expires_at=expires_at,
                    entity_registry=registry,
                )
            finally:
                session.close()

            print("=" * 70)
            print("✓ Created agent WITH API key")
            print("=" * 70)
            print(f"  Owner (user_id):    {args.user_id}")
            print(f"  Agent (agent_id):   {args.agent_id}")
            print(f"  Name:               {args.name}")
            if expires_at:
                print(f"  Key expires:        {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            else:
                print("  Key expires:        Never")
            print()
            print("⚠️  IMPORTANT: Save this key - it will not be shown again!")
            print("=" * 70)
            print()
            print(f"  API Key: {raw_key}")
            print()
            print("=" * 70)
            print()
            print("Usage:")
            print(f"  export NEXUS_API_KEY='{raw_key}'")
            print("  nexus ls /workspace --remote-url http://localhost:8080")
            print()
        else:
            # Register agent WITHOUT API key (v0.5.0 recommended approach)
            register_agent(
                user_id=args.user_id,
                agent_id=args.agent_id,
                name=args.name,
                tenant_id=args.tenant_id,
                entity_registry=registry,
            )

            print("=" * 70)
            print("✓ Registered agent (NO API key)")
            print("=" * 70)
            print(f"  Owner (user_id):    {args.user_id}")
            print(f"  Agent (agent_id):   {args.agent_id}")
            print(f"  Name:               {args.name}")
            print()
            print("✅ This agent uses user's authentication + X-Agent-ID header")
            print("=" * 70)
            print()
            print("Usage:")
            print("  # User authenticates, agent identity declared via header")
            print(f"  export NEXUS_API_KEY='<{args.user_id}_api_key>'")
            print("  curl -H 'Authorization: Bearer $NEXUS_API_KEY' \\")
            print(f"       -H 'X-Agent-ID: {args.agent_id}' \\")
            print("       http://localhost:8080/api/nfs/list")
            print()

        print("Agent Identity:")
        print(f"  - Owned by user: {args.user_id}")
        print(f"  - Inherits permissions from user '{args.user_id}'")
        print("  - Can be granted additional permissions via ReBAC")
        print()
        print("Next steps:")
        print("  1. Grant agent-specific permissions (optional):")
        print(f"     nexus rebac create agent {args.agent_id} editor file /data")
        print()

    except Exception as e:
        print(f"❌ Error creating agent: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
