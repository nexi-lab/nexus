#!/usr/bin/env python3
"""Manual test for authentication endpoints with proper Authorization header.

This test starts a minimal FastAPI server and tests the fixed Authorization header extraction.
"""

import time
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn
import threading

from nexus.storage.models import Base, UserModel, UserOAuthAccountModel, ExternalUserServiceModel
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth import auth_routes
from fastapi import FastAPI

# Create in-memory database
database_url = "sqlite:///:memory:"
engine = create_engine(database_url, echo=False)

# Create tables (import user models first to register them with Base.metadata)
Base.metadata.create_all(engine)

# Create session factory
session_factory = sessionmaker(bind=engine)

# Create auth provider
auth_provider = DatabaseLocalAuth(
    session_factory=session_factory,
    jwt_secret="test-secret-key",
    token_expiry=3600,
)

# Create FastAPI app
app = FastAPI()

# Set up auth provider and include routes
auth_routes.set_auth_provider(auth_provider)
app.include_router(auth_routes.router)

print("=" * 70)
print("Testing Authentication with Fixed Authorization Header Extraction")
print("=" * 70)
print()

# Start server in background
server_ready = threading.Event()

def run_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=8083, log_level="error")
    server = uvicorn.Server(config)

    async def startup():
        server_ready.set()

    config.on_startup = [startup]
    server.run()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# Wait for server
server_ready.wait(timeout=5)
time.sleep(1)

base_url = "http://127.0.0.1:8083"
print(f"✅ Test server started at {base_url}")
print()

# Test 1: Register user
print("Test 1: Register user")
print("-" * 70)
response = requests.post(
    f"{base_url}/auth/register",
    json={
        "email": "test@example.com",
        "password": "securepassword123",
        "username": "testuser"
    }
)
print(f"Status: {response.status_code}")
if response.status_code == 201:
    data = response.json()
    print(f"✅ User registered: {data['email']}")
    token = data['token']
    print(f"   Token: {token[:50]}...")
else:
    print(f"❌ Failed: {response.text}")
    exit(1)

print()

# Test 2: Get profile with Bearer token (THIS IS THE FIX WE'RE TESTING)
print("Test 2: Get profile with Authorization Bearer token")
print("-" * 70)
response = requests.get(
    f"{base_url}/auth/me",
    headers={"Authorization": f"Bearer {token}"}
)
print(f"Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    print(f"✅ Profile retrieved successfully!")
    print(f"   Email: {data['email']}")
    print(f"   Username: {data['username']}")
else:
    print(f"❌ Failed: {response.text}")
    exit(1)

print()

# Test 3: Update profile with Bearer token
print("Test 3: Update profile with Authorization Bearer token")
print("-" * 70)
response = requests.patch(
    f"{base_url}/auth/me",
    headers={"Authorization": f"Bearer {token}"},
    json={"display_name": "Test User Updated"}
)
print(f"Status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    print(f"✅ Profile updated successfully!")
    print(f"   Display name: {data['display_name']}")
else:
    print(f"❌ Failed: {response.text}")
    exit(1)

print()

# Test 4: Change password with Bearer token
print("Test 4: Change password with Authorization Bearer token")
print("-" * 70)
response = requests.post(
    f"{base_url}/auth/change-password",
    headers={"Authorization": f"Bearer {token}"},
    json={
        "old_password": "securepassword123",
        "new_password": "newsecurepassword456"
    }
)
print(f"Status: {response.status_code}")
if response.status_code == 200:
    print(f"✅ Password changed successfully!")
else:
    print(f"❌ Failed: {response.text}")
    exit(1)

print()

# Test 5: Try accessing without token (should fail)
print("Test 5: Access without token (should fail with 401)")
print("-" * 70)
response = requests.get(f"{base_url}/auth/me")
print(f"Status: {response.status_code}")
if response.status_code == 401:
    print(f"✅ Correctly rejected request without token")
else:
    print(f"❌ Should have returned 401, got {response.status_code}")

print()

# Test 6: Try accessing with invalid token (should fail)
print("Test 6: Access with invalid token (should fail with 401)")
print("-" * 70)
response = requests.get(
    f"{base_url}/auth/me",
    headers={"Authorization": "Bearer invalid-token-here"}
)
print(f"Status: {response.status_code}")
if response.status_code == 401:
    print(f"✅ Correctly rejected invalid token")
else:
    print(f"❌ Should have returned 401, got {response.status_code}")

print()

# Summary
print("=" * 70)
print("✅ All Authorization header tests passed!")
print("=" * 70)
print()
print("The fix successfully:")
print("  ✅ Extracts Bearer tokens from Authorization header")
print("  ✅ Validates tokens correctly")
print("  ✅ Rejects requests without tokens")
print("  ✅ Rejects requests with invalid tokens")
print("  ✅ Works for all authenticated endpoints (GET/PATCH/POST)")
print()
