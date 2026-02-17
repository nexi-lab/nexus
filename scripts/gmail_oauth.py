#!/usr/bin/env python3
"""Quick Gmail OAuth token exchange."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.server.auth.oauth_factory import OAuthProviderFactory
from nexus.server.auth.token_manager import TokenManager


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/gmail_oauth.py <code>")
        print("Get code from: http://localhost:5173/oauth/callback?code=<CODE>")
        sys.exit(1)

    code = sys.argv[1]

    factory = OAuthProviderFactory()
    provider = factory.create_provider("gmail")

    cred = await provider.exchange_code(code)

    db_path = Path.home() / ".nexus" / "nexus.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    tm = TokenManager(db_path=str(db_path))
    tm.store_tokens(
        user_id=cred.user_id,
        provider="gmail",
        access_token=cred.access_token,
        refresh_token=cred.refresh_token,
        expires_at=cred.expires_at,
    )
    print(f"Saved tokens for: {cred.user_id}")


asyncio.run(main())
