#!/usr/bin/env python3
"""Quick OAuth script for Google Calendar.

Usage:
    python scripts/oauth_gcalendar.py
"""

import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Add src to path
sys.path.insert(0, str(__file__).replace("/scripts/oauth_gcalendar.py", "/src"))

from pathlib import Path

from google_auth_oauthlib.flow import Flow

# OAuth configuration (set via environment variables)
CLIENT_ID = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    print("Error: Set NEXUS_OAUTH_GOOGLE_CLIENT_ID and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")
    print("Get credentials from: https://console.cloud.google.com/apis/credentials")
    sys.exit(1)
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]
REDIRECT_URI = "http://localhost:8085"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback."""

    def do_GET(self):
        """Handle the OAuth callback."""
        query = parse_qs(urlparse(self.path).query)

        if "code" in query:
            self.server.auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                </body></html>
            """)
        else:
            self.server.auth_code = None
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            error = query.get("error", ["Unknown error"])[0]
            self.wfile.write(f"<html><body><h1>Error: {error}</h1></body></html>".encode())

    def log_message(self, format, *args):
        pass  # Suppress logging


def main():
    print("=" * 60)
    print("  Google Calendar OAuth Setup")
    print("=" * 60)
    print()

    # Create OAuth flow
    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI

    # Generate authorization URL
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    print("Opening browser for authorization...")
    print(f"If browser doesn't open, visit:\n{auth_url}\n")

    # Start local server to receive callback
    server = HTTPServer(("localhost", 8085), OAuthCallbackHandler)
    server.auth_code = None

    # Open browser
    webbrowser.open(auth_url)

    print("Waiting for authorization...")

    # Wait for callback
    while server.auth_code is None:
        server.handle_request()

    print("Authorization received! Exchanging for tokens...")

    # Exchange code for tokens
    flow.fetch_token(code=server.auth_code)
    credentials = flow.credentials

    # Save to database
    import asyncio

    from nexus.server.auth.oauth_provider import OAuthCredential
    from nexus.server.auth.token_manager import TokenManager

    db_path = Path.home() / ".nexus" / "nexus.db"
    tm = TokenManager(str(db_path))

    # Get user email from credentials
    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=credentials)
    calendar = service.calendars().get(calendarId="primary").execute()
    user_email = calendar.get("id", "unknown@gmail.com")

    # Create OAuthCredential
    oauth_cred = OAuthCredential(
        access_token=credentials.token,
        refresh_token=credentials.refresh_token,
        expires_at=credentials.expiry,
        scopes=SCOPES,
        token_type="Bearer",
    )

    # Store credential
    asyncio.run(
        tm.store_credential(
            provider="gcalendar",
            user_email=user_email,
            credential=oauth_cred,
        )
    )

    print()
    print("=" * 60)
    print(f"  SUCCESS! Token stored for: {user_email}")
    print("=" * 60)
    print()
    print("You can now run the E2E tests:")
    print(f"  python scripts/test_gcalendar_e2e.py --user {user_email}")
    print()


if __name__ == "__main__":
    main()
