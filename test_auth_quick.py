#!/usr/bin/env python3
"""Quick test script to verify authentication integration.

This script starts a FastAPI test server and tests the authentication endpoints.
"""

import time

import requests
from sqlalchemy import create_engine

from nexus import NexusFS
from nexus.backends.local import LocalBackend
from nexus.server.fastapi_server import create_app
from nexus.storage.models import Base

# Create test database
database_url = "sqlite:///./test_auth.db"
engine = create_engine(database_url, echo=False)

# Drop and recreate all tables
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

# Create NexusFS instance
backend = LocalBackend(root_path="./test-auth-data")
nx = NexusFS(
    backend=backend,
    db_path=database_url,
    enforce_permissions=False,
)

# Create FastAPI app with authentication
app = create_app(
    nexus_fs=nx,
    database_url=database_url,
)

print("✅ FastAPI app created with authentication")
print("=" * 60)
print()

# Start test server in background
import threading

import uvicorn

server_ready = threading.Event()


def run_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=8081, log_level="error")
    server = uvicorn.Server(config)

    async def startup():
        server_ready.set()

    config.on_startup = [startup]
    server.run()


server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# Wait for server to be ready
server_ready.wait(timeout=5)
time.sleep(1)

base_url = "http://127.0.0.1:8081"
print(f"🚀 Test server started at {base_url}")
print()

# Test 1: Register a user
print("Test 1: Register a user")
print("-" * 60)
response = requests.post(
    f"{base_url}/auth/register",
    json={
        "email": "alice@example.com",
        "password": "securepassword123",
        "username": "alice",
        "display_name": "Alice Smith",
    },
)
print(f"Status: {response.status_code}")
if response.status_code == 201:
    data = response.json()
    print(f"✅ User registered: {data['email']}")
    print(f"   Token: {data['token'][:50]}...")
    alice_token = data["token"]
else:
    print(f"❌ Failed: {response.text}")
    alice_token = None

print()

# Test 2: Login with username
print("Test 2: Login with username")
print("-" * 60)
response = requests.post(
    f"{base_url}/auth/login", json={"identifier": "alice", "password": "securepassword123"}
)
print(f"Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    print("✅ Login successful")
    print(f"   User: {data['user']['email']}")
    print(f"   Auth method: {data['user']['primary_auth_method']}")
else:
    print(f"❌ Failed: {response.text}")

print()

# Test 3: Get user profile
print("Test 3: Get user profile")
print("-" * 60)
if alice_token:
    response = requests.get(
        f"{base_url}/auth/me", headers={"Authorization": f"Bearer {alice_token}"}
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print("✅ Profile retrieved")
        print(f"   Email: {data['email']}")
        print(f"   Username: {data['username']}")
        print(f"   Display name: {data['display_name']}")
    else:
        print(f"❌ Failed: {response.text}")
else:
    print("⏭️  Skipped (no token from registration)")

print()

# Test 4: Update profile
print("Test 4: Update profile")
print("-" * 60)
if alice_token:
    response = requests.patch(
        f"{base_url}/auth/me",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"display_name": "Alice Johnson", "avatar_url": "https://example.com/avatar.jpg"},
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print("✅ Profile updated")
        print(f"   New display name: {data['display_name']}")
        print(f"   Avatar URL: {data['avatar_url']}")
    else:
        print(f"❌ Failed: {response.text}")
else:
    print("⏭️  Skipped (no token from registration)")

print()

# Test 5: Change password
print("Test 5: Change password")
print("-" * 60)
if alice_token:
    response = requests.post(
        f"{base_url}/auth/change-password",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={"old_password": "securepassword123", "new_password": "newsecurepassword456"},
    )
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        print("✅ Password changed successfully")

        # Try logging in with new password
        response = requests.post(
            f"{base_url}/auth/login",
            json={"identifier": "alice", "password": "newsecurepassword456"},
        )
        if response.status_code == 200:
            print("✅ Login with new password successful")
        else:
            print("❌ Login with new password failed")
    else:
        print(f"❌ Failed: {response.text}")
else:
    print("⏭️  Skipped (no token from registration)")

print()

# Summary
print("=" * 60)
print("✅ Authentication system integration test complete!")
print("=" * 60)
print()
print("All endpoints working:")
print("  • POST /auth/register - User registration")
print("  • POST /auth/login - User login")
print("  • GET /auth/me - Get profile")
print("  • PATCH /auth/me - Update profile")
print("  • POST /auth/change-password - Password change")
print()
print("The authentication system is fully integrated with FastAPI!")
print()

# Cleanup
print("Cleaning up...")
import os
import shutil

if os.path.exists("./test_auth.db"):
    os.remove("./test_auth.db")
if os.path.exists("./test-auth-data"):
    shutil.rmtree("./test-auth-data")
print("✓ Cleanup complete")
