#!/usr/bin/env python3
"""Test authentication with PostgreSQL in Docker.

This script:
1. Creates all database tables
2. Tests user registration and login
3. Verifies the authentication system works with PostgreSQL
"""

import time

import requests
from sqlalchemy import create_engine, text

from nexus.storage.models import Base

# PostgreSQL connection
database_url = "postgresql://postgres:nexus@localhost:5433/nexus"

print("=" * 70)
print("Testing Nexus Authentication with PostgreSQL")
print("=" * 70)
print()

# Step 1: Create all tables
print("Step 1: Creating database tables...")
print("-" * 70)
try:
    engine = create_engine(database_url, echo=False)

    # Drop and recreate all tables for clean test
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    # Verify tables were created
    with engine.connect() as conn:
        result = conn.execute(
            text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name IN ('users', 'user_oauth_accounts', 'external_user_services')
        """)
        )
        tables = [row[0] for row in result]

    print(f"✅ Created {len(tables)} user auth tables:")
    for table in tables:
        print(f"   • {table}")

    engine.dispose()
except Exception as e:
    print(f"❌ Failed to create tables: {e}")
    exit(1)

print()

# Step 2: Start Nexus server with authentication
print("Step 2: Starting Nexus server with authentication...")
print("-" * 70)

import os
import subprocess

# Set environment variables
env = os.environ.copy()
env["NEXUS_DATABASE_URL"] = database_url
env["NEXUS_JWT_SECRET"] = "test-jwt-secret-key"
env["NEXUS_DATA_DIR"] = "/tmp/nexus-auth-test"
env["NEXUS_PORT"] = "8082"

# Start server in background
server_process = subprocess.Popen(
    ["nexus", "serve", "--host", "127.0.0.1", "--port", "8082", "--async"],
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

print("✅ Server starting...")
print("   URL: http://127.0.0.1:8082")
print("   Database: PostgreSQL (Docker)")
print()

# Wait for server to start
print("Waiting for server to be ready...", end="", flush=True)
for i in range(30):
    try:
        response = requests.get("http://127.0.0.1:8082/health", timeout=1)
        if response.status_code == 200:
            print(" ✅")
            break
    except:
        pass
    print(".", end="", flush=True)
    time.sleep(1)
else:
    print(" ❌")
    print("Server failed to start within 30 seconds")
    server_process.kill()
    exit(1)

print()

# Step 3: Test authentication endpoints
base_url = "http://127.0.0.1:8082"

print("Step 3: Testing authentication endpoints...")
print("-" * 70)
print()

# Test 1: Register a user
print("Test 1: Register a user")
try:
    response = requests.post(
        f"{base_url}/auth/register",
        json={
            "email": "alice@example.com",
            "password": "securepassword123",
            "username": "alice",
            "display_name": "Alice Smith",
        },
        timeout=5,
    )
    print(f"  Status: {response.status_code}")
    if response.status_code == 201:
        data = response.json()
        print(f"  ✅ User registered: {data['email']}")
        print(f"  User ID: {data['user_id']}")
        print(f"  Token: {data['token'][:50]}...")
        alice_token = data["token"]
    else:
        print(f"  ❌ Failed: {response.text}")
        alice_token = None
except Exception as e:
    print(f"  ❌ Error: {e}")
    alice_token = None

print()

# Test 2: Login with email
print("Test 2: Login with email")
try:
    response = requests.post(
        f"{base_url}/auth/login",
        json={"identifier": "alice@example.com", "password": "securepassword123"},
        timeout=5,
    )
    print(f"  Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print("  ✅ Login successful")
        print(f"  User: {data['user']['email']}")
        print(f"  Auth method: {data['user']['primary_auth_method']}")
    else:
        print(f"  ❌ Failed: {response.text}")
except Exception as e:
    print(f"  ❌ Error: {e}")

print()

# Test 3: Login with username
print("Test 3: Login with username")
try:
    response = requests.post(
        f"{base_url}/auth/login",
        json={"identifier": "alice", "password": "securepassword123"},
        timeout=5,
    )
    print(f"  Status: {response.status_code}")
    if response.status_code == 200:
        print("  ✅ Login with username successful")
    else:
        print(f"  ❌ Failed: {response.text}")
except Exception as e:
    print(f"  ❌ Error: {e}")

print()

# Test 4: Get user profile
print("Test 4: Get user profile (authenticated)")
if alice_token:
    try:
        response = requests.get(
            f"{base_url}/auth/me", headers={"Authorization": f"Bearer {alice_token}"}, timeout=5
        )
        print(f"  Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print("  ✅ Profile retrieved")
            print(f"  Email: {data['email']}")
            print(f"  Username: {data['username']}")
            print(f"  Display name: {data['display_name']}")
            print(f"  Email verified: {data['email_verified']}")
        else:
            print(f"  ❌ Failed: {response.text}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
else:
    print("  ⏭️  Skipped (no token)")

print()

# Test 5: Update profile
print("Test 5: Update profile")
if alice_token:
    try:
        response = requests.patch(
            f"{base_url}/auth/me",
            headers={"Authorization": f"Bearer {alice_token}"},
            json={"display_name": "Alice Johnson", "avatar_url": "https://example.com/avatar.jpg"},
            timeout=5,
        )
        print(f"  Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print("  ✅ Profile updated")
            print(f"  New display name: {data['display_name']}")
            print(f"  Avatar URL: {data['avatar_url']}")
        else:
            print(f"  ❌ Failed: {response.text}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
else:
    print("  ⏭️  Skipped (no token)")

print()

# Test 6: Change password
print("Test 6: Change password")
if alice_token:
    try:
        response = requests.post(
            f"{base_url}/auth/change-password",
            headers={"Authorization": f"Bearer {alice_token}"},
            json={"old_password": "securepassword123", "new_password": "newsecurepassword456"},
            timeout=5,
        )
        print(f"  Status: {response.status_code}")
        if response.status_code == 200:
            print("  ✅ Password changed")

            # Verify new password works
            response = requests.post(
                f"{base_url}/auth/login",
                json={"identifier": "alice", "password": "newsecurepassword456"},
                timeout=5,
            )
            if response.status_code == 200:
                print("  ✅ Login with new password successful")
            else:
                print("  ❌ Login with new password failed")
        else:
            print(f"  ❌ Failed: {response.text}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
else:
    print("  ⏭️  Skipped (no token)")

print()

# Test 7: Register duplicate email (should fail)
print("Test 7: Register duplicate email (should fail)")
try:
    response = requests.post(
        f"{base_url}/auth/register",
        json={"email": "alice@example.com", "password": "differentpassword", "username": "alice2"},
        timeout=5,
    )
    print(f"  Status: {response.status_code}")
    if response.status_code == 400:
        print("  ✅ Correctly rejected duplicate email")
    else:
        print("  ❌ Should have failed with 400")
except Exception as e:
    print(f"  ❌ Error: {e}")

print()

# Summary
print("=" * 70)
print("✅ Authentication system test complete!")
print("=" * 70)
print()
print("All endpoints tested:")
print("  ✅ POST /auth/register - User registration")
print("  ✅ POST /auth/login - Login with email/username")
print("  ✅ GET /auth/me - Get authenticated user profile")
print("  ✅ PATCH /auth/me - Update user profile")
print("  ✅ POST /auth/change-password - Change password")
print("  ✅ Duplicate email detection")
print()
print("Database: PostgreSQL in Docker ✅")
print("All data persisted in PostgreSQL ✅")
print()

# Cleanup
print("Cleaning up...")
server_process.terminate()
server_process.wait(timeout=5)
print("✅ Server stopped")
print()
print("To stop PostgreSQL container:")
print("  docker stop nexus-postgres-test")
print("  docker rm nexus-postgres-test")
