"""X (Twitter) Connector Backend Example.

This example demonstrates how to use the X connector backend
with OAuth 2.0 PKCE authentication for social media integration.

Prerequisites:
    1. Install dependencies:
       pip install httpx python-dotenv

    2. Setup OAuth credentials in .env file (root directory):
       NEXUS_OAUTH_X_CLIENT_ID="your-client-id"
       NEXUS_OAUTH_X_CLIENT_SECRET="your-client-secret"  # Optional for PKCE
       NEXUS_USER_EMAIL="your-email@example.com"

    3. Run OAuth setup:
       nexus oauth setup-x --user-email "your-email@example.com"
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Load environment variables from .env file before importing nexus modules
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from nexus.backends import XConnectorBackend  # noqa: E402
from nexus.core.exceptions import AuthenticationError, BackendError  # noqa: E402
from nexus.core.permissions import OperationContext  # noqa: E402


async def main():
    """Main example function."""
    print("=" * 60)
    print("X (Twitter) Connector Backend Example")
    print("=" * 60)

    # 1. Setup paths
    print("\n[1/7] Initializing X connector...")
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")

    # 2. Create X connector backend
    backend = XConnectorBackend(
        token_manager_db=db_path,
        cache_ttl={"timeline": 300, "mentions": 300},  # 5 min cache
    )
    print(f"✓ Backend created (name: {backend.name})")
    print(f"✓ User-scoped: {backend.user_scoped}")

    # 3. Setup operation context
    print("\n[2/7] Setting up operation context...")
    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")

    context = OperationContext(
        user=user_email,
        groups=[],
        tenant_id="example_org",
        backend_path="/x/timeline/recent.json",
    )
    print("✓ Context created:")
    print(f"  - User: {context.user}")
    print(f"  - Tenant: {context.tenant_id}")
    print(f"  - Path: {context.backend_path}")

    # 4. Read timeline
    print("\n[3/7] Reading home timeline...")
    try:
        timeline_json = backend.read_content("", context=context)
        timeline = json.loads(timeline_json)

        tweet_count = len(timeline.get("data", []))
        print("✓ Timeline fetched successfully")
        print(f"  - Tweets: {tweet_count}")

        # Show first 3 tweets
        if timeline.get("data"):
            print("\n  Recent tweets:")
            for i, tweet in enumerate(timeline["data"][:3], 1):
                author = tweet.get("author_id", "unknown")
                text = tweet.get("text", "")[:60]
                print(f"    {i}. @{author}: {text}...")

    except AuthenticationError as e:
        print(f"✗ Authentication failed: {e}")
        print("  Setup OAuth first:")
        print("  1. Get credentials from https://developer.twitter.com/")
        print("  2. Run: nexus oauth setup-x --client-id ... --user-email ...")
        return
    except BackendError as e:
        print(f"✗ Backend error: {e}")
        return

    # 5. Read mentions
    print("\n[4/7] Reading mentions...")
    try:
        context.backend_path = "/x/mentions/recent.json"
        mentions_json = backend.read_content("", context=context)
        mentions = json.loads(mentions_json)

        mention_count = len(mentions.get("data", []))
        print("✓ Mentions fetched successfully")
        print(f"  - Mentions: {mention_count}")

    except Exception as e:
        print(f"✗ Failed to read mentions: {e}")

    # 6. Post a tweet
    print("\n[5/7] Posting a tweet...")
    try:
        context.backend_path = "/x/posts/new.json"
        tweet_data = {"text": "Hello from Nexus! Testing the X connector 🚀"}
        tweet_json = json.dumps(tweet_data).encode("utf-8")

        tweet_id = backend.write_content(tweet_json, context=context)
        print("✓ Tweet posted successfully")
        print(f"  - Tweet ID: {tweet_id}")
        print(f"  - URL: https://twitter.com/i/web/status/{tweet_id}")

    except PermissionError as e:
        print(f"✗ Permission error: {e}")
    except Exception as e:
        print(f"✗ Failed to post tweet: {e}")

    # 7. Search tweets
    print("\n[6/7] Searching tweets...")
    try:
        context.backend_path = "/x/search/python_programming.json"
        search_json = backend.read_content("", context=context)
        search_results = json.loads(search_json)

        result_count = len(search_results.get("data", []))
        print("✓ Search completed successfully")
        print(f"  - Results: {result_count}")

    except Exception as e:
        print(f"✗ Search failed: {e}")

    # 8. List directories
    print("\n[7/7] Listing virtual directories...")
    try:
        dirs = backend.list_dir("x", context=context)
        print("✓ Listed root directories:")
        for dir_name in dirs:
            print(f"    - {dir_name}")

    except Exception as e:
        print(f"✗ Failed to list directories: {e}")

    # Cleanup
    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Check X timeline in web UI")
    print("2. Try: context.backend_path = '/x/posts/all.json'")
    print("3. Use with NexusFS for full filesystem operations")


async def grep_example():
    """Example: Search tweets using grep."""
    print("\n" + "=" * 60)
    print("X Connector Grep Example")
    print("=" * 60)

    # Setup
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")
    backend = XConnectorBackend(token_manager_db=db_path)

    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")
    context = OperationContext(
        user=user_email,
        groups=[],
        tenant_id="example_org",
    )

    # Grep for tweets containing "python"
    print("\n[1/2] Searching for tweets containing 'python'...")
    try:
        results = backend.grep("python", path="/x/search/", context=context, max_results=10)
        print(f"✓ Found {len(results)} matches")

        for i, match in enumerate(results[:5], 1):
            print(f"\n  Match {i}:")
            print(f"    File: {match['file']}")
            print(f"    Line: {match['line']}")
            print(f"    Content: {match['content'][:60]}...")

    except Exception as e:
        print(f"✗ Grep failed: {e}")

    # Grep cached timeline
    print("\n[2/2] Searching cached timeline...")
    try:
        # First, read timeline to cache it
        context.backend_path = "/x/timeline/recent.json"
        backend.read_content("", context=context)

        # Now grep the cache
        results = backend.grep("AI", path="/x/timeline/", context=context)
        print(f"✓ Found {len(results)} matches in cached timeline")

    except Exception as e:
        print(f"✗ Grep failed: {e}")

    print("\n" + "=" * 60)
    print("Grep example completed!")
    print("=" * 60)


async def glob_example():
    """Example: List files using glob patterns."""
    print("\n" + "=" * 60)
    print("X Connector Glob Example")
    print("=" * 60)

    # Setup
    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")
    backend = XConnectorBackend(token_manager_db=db_path)

    user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")
    context = OperationContext(
        user=user_email,
        groups=[],
        tenant_id="example_org",
    )

    # Glob root directories
    print("\n[1/3] Listing root directories...")
    try:
        paths = backend.glob("/x/*", context=context)
        print(f"✓ Found {len(paths)} directories:")
        for path in paths:
            print(f"    - {path}")

    except Exception as e:
        print(f"✗ Glob failed: {e}")

    # Glob timeline files
    print("\n[2/3] Listing timeline files...")
    try:
        paths = backend.glob("/x/timeline/*.json", context=context)
        print(f"✓ Found {len(paths)} timeline files:")
        for path in paths:
            print(f"    - {path}")

    except Exception as e:
        print(f"✗ Glob failed: {e}")

    # Glob posts
    print("\n[3/3] Listing posts...")
    try:
        paths = backend.glob("/x/posts/*.json", context=context)
        print(f"✓ Found {len(paths)} posts:")
        for path in paths[:5]:  # Show first 5
            print(f"    - {path}")

    except Exception as e:
        print(f"✗ Glob failed: {e}")

    print("\n" + "=" * 60)
    print("Glob example completed!")
    print("=" * 60)


if __name__ == "__main__":
    # Run main example (synchronous - backend handles async internally)

    # The X connector uses asyncio.run() internally, so we call main() directly
    # without wrapping in asyncio.run()
    def run_sync():
        """Run examples synchronously."""
        print("=" * 60)
        print("X (Twitter) Connector Backend Example")
        print("=" * 60)

        # 1. Setup paths
        print("\n[1/7] Initializing X connector...")
        home = os.path.expanduser("~")
        db_path = os.path.join(home, ".nexus", "nexus.db")

        # 2. Create X connector backend
        backend = XConnectorBackend(
            token_manager_db=db_path,
            cache_ttl={"timeline": 300, "mentions": 300},  # 5 min cache
        )
        print(f"✓ Backend created (name: {backend.name})")
        print(f"✓ User-scoped: {backend.user_scoped}")

        # 3. Setup operation context
        print("\n[2/7] Setting up operation context...")
        user_email = os.getenv("NEXUS_USER_EMAIL", "your-email@example.com")

        context = OperationContext(
            user=user_email,
            groups=[],
            tenant_id="default",  # Must match the tenant_id used during OAuth setup
            backend_path="/x/timeline/recent.json",
        )
        print("✓ Context created:")
        print(f"  - User: {context.user}")
        print(f"  - Tenant: {context.tenant_id}")
        print(f"  - Path: {context.backend_path}")

        # 4. Read timeline
        print("\n[3/7] Reading home timeline...")
        try:
            timeline_json = backend.read_content("", context=context)
            timeline = json.loads(timeline_json)

            tweet_count = len(timeline.get("data", []))
            print("✓ Timeline fetched successfully")
            print(f"  - Tweets: {tweet_count}")

            # Show first 3 tweets
            if timeline.get("data"):
                print("\n  Recent tweets:")
                for i, tweet in enumerate(timeline["data"][:3], 1):
                    author = tweet.get("author_id", "unknown")
                    text = tweet.get("text", "")[:60]
                    print(f"    {i}. @{author}: {text}...")

        except AuthenticationError as e:
            print(f"✗ Authentication failed: {e}")
            print("  Setup OAuth first:")
            print("  1. Get credentials from https://developer.twitter.com/")
            print("  2. Run: python setup_x_oauth.py")
            return
        except BackendError as e:
            print(f"✗ Backend error: {e}")
            return

        # 5. Read mentions
        print("\n[4/7] Reading mentions...")
        try:
            context.backend_path = "/x/mentions/recent.json"
            mentions_json = backend.read_content("", context=context)
            mentions = json.loads(mentions_json)

            mention_count = len(mentions.get("data", []))
            print("✓ Mentions fetched successfully")
            print(f"  - Mentions: {mention_count}")

        except Exception as e:
            print(f"✗ Failed to read mentions: {e}")

        # 6. Skip posting a tweet (commented out to avoid posting)
        print("\n[5/7] Posting a tweet... (SKIPPED - uncomment to test)")
        # try:
        #     context.backend_path = "/x/posts/new.json"
        #     tweet_data = {
        #         "text": "Hello from Nexus! Testing the X connector 🚀"
        #     }
        #     tweet_json = json.dumps(tweet_data).encode("utf-8")
        #
        #     tweet_id = backend.write_content(tweet_json, context=context)
        #     print(f"✓ Tweet posted successfully")
        #     print(f"  - Tweet ID: {tweet_id}")
        #     print(f"  - URL: https://twitter.com/i/web/status/{tweet_id}")
        #
        # except PermissionError as e:
        #     print(f"✗ Permission error: {e}")
        # except Exception as e:
        #     print(f"✗ Failed to post tweet: {e}")

        # 7. Search tweets
        print("\n[6/7] Searching tweets...")
        try:
            context.backend_path = "/x/search/python_programming.json"
            search_json = backend.read_content("", context=context)
            search_results = json.loads(search_json)

            result_count = len(search_results.get("data", []))
            print("✓ Search completed successfully")
            print(f"  - Results: {result_count}")

        except Exception as e:
            print(f"✗ Search failed: {e}")

        # 8. List directories
        print("\n[7/7] Listing virtual directories...")
        try:
            dirs = backend.list_dir("x", context=context)
            print("✓ Listed root directories:")
            for dir_name in dirs:
                print(f"    - {dir_name}")

        except Exception as e:
            print(f"✗ Failed to list directories: {e}")

        # Cleanup
        print("\n" + "=" * 60)
        print("Example completed!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Check X timeline in web UI")
        print("2. Try: context.backend_path = '/x/posts/all.json'")
        print("3. Use with NexusFS for full filesystem operations")

    run_sync()
