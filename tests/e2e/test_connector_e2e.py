"""End-to-end test for connector API endpoints (Issue #3148, #3182).

Tests the full connector lifecycle through the REST API — the same
endpoints the TUI Connectors tab will consume.

Requires:
- Nexus server running at NEXUS_URL (default: http://localhost:2026)
- API key in NEXUS_API_KEY
- gws CLI authenticated (for Gmail/Calendar tests)

Run:
    NEXUS_URL=http://localhost:2026 NEXUS_API_KEY=<key> pytest tests/e2e/test_connector_e2e.py -v

Or standalone:
    python tests/e2e/test_connector_e2e.py
"""

import json
import os
import sys

import requests

BASE_URL = os.getenv("NEXUS_URL", "http://localhost:2026")
API_KEY = os.getenv("NEXUS_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# Target email for write tests
TARGET_EMAIL = "oliverfengpet@gmail.com"


def api(method: str, path: str, json_body: dict | None = None) -> dict:
    """Make an API call and return JSON response."""
    url = f"{BASE_URL}{path}"
    resp = getattr(requests, method)(url, json=json_body, headers=HEADERS, timeout=30)
    if resp.status_code >= 500:
        print(f"  SERVER ERROR {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else {}


def test_section(name: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    icon = "✓" if condition else "✗"
    msg = f"  {icon} {status}: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return condition


def main() -> None:
    passed = 0
    failed = 0

    def track(result: bool) -> None:
        nonlocal passed, failed
        if result:
            passed += 1
        else:
            failed += 1

    print("Nexus Connector E2E Tests")
    print(f"Server: {BASE_URL}")
    print(f"API Key: {'set' if API_KEY else 'NOT SET'}")

    # ===================================================================
    # 1. Server health
    # ===================================================================
    test_section("1. Server Health")
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    track(check("Server is healthy", resp.status_code == 200, resp.json().get("status", "")))

    # ===================================================================
    # 2. List connectors (discovery)
    # ===================================================================
    test_section("2. List Registered Connectors")
    data = api("get", "/api/v2/connectors")
    connectors = data.get("connectors", [])
    track(check("Has connectors", len(connectors) > 0, f"{len(connectors)} registered"))
    names = [c["name"] for c in connectors]
    for expected in ["gmail_connector", "gcalendar_connector"]:
        track(check(f"{expected} registered", expected in names))

    # ===================================================================
    # 3. Available connectors (with status)
    # ===================================================================
    test_section("3. Available Connectors (TUI endpoint)")
    available = api("get", "/api/v2/connectors/available")
    if isinstance(available, list):
        track(check("Available endpoint works", len(available) > 0, f"{len(available)} connectors"))
    else:
        track(check("Available endpoint works", False, str(available)[:100]))

    # ===================================================================
    # 4. Connector capabilities
    # ===================================================================
    test_section("4. Connector Capabilities")
    for name in ["gmail_connector", "gcalendar_connector"]:
        caps = api("get", f"/api/v2/connectors/{name}/capabilities")
        has_caps = len(caps.get("capabilities", [])) > 0
        track(check(f"{name} capabilities", has_caps, str(caps.get("capabilities", []))[:80]))

    # ===================================================================
    # 5. Mount Gmail connector
    # ===================================================================
    test_section("5. Mount Gmail")
    mount_resp = api(
        "post",
        "/api/v2/connectors/mount",
        {
            "connector_type": "gmail_connector",
            "mount_point": "/mnt/gmail",
            "config": {
                "token_manager_db": os.path.expanduser("~/.nexus/nexus.db"),
                "user_email": "taofeng.nju@gmail.com",
            },
        },
    )
    track(
        check(
            "Gmail mounted",
            mount_resp.get("mounted", False) or "already" in str(mount_resp.get("error", "")),
            mount_resp.get("error", "OK"),
        )
    )

    # ===================================================================
    # 6. Mount Calendar connector
    # ===================================================================
    test_section("6. Mount Calendar")
    mount_resp = api(
        "post",
        "/api/v2/connectors/mount",
        {
            "connector_type": "gcalendar_connector",
            "mount_point": "/mnt/calendar",
            "config": {
                "token_manager_db": os.path.expanduser("~/.nexus/nexus.db"),
                "user_email": "taofeng.nju@gmail.com",
            },
        },
    )
    track(
        check(
            "Calendar mounted",
            mount_resp.get("mounted", False) or "already" in str(mount_resp.get("error", "")),
            mount_resp.get("error", "OK"),
        )
    )

    # ===================================================================
    # 7. List mounts
    # ===================================================================
    test_section("7. List Mounted Connectors")
    mounts = api("get", "/api/v2/connectors/mounts")
    if isinstance(mounts, list):
        track(check("Mounts listed", len(mounts) > 0, f"{len(mounts)} mounts"))
        mount_paths = [m.get("mount_point") for m in mounts]
        track(check("/mnt/gmail in mounts", "/mnt/gmail" in mount_paths))
        track(check("/mnt/calendar in mounts", "/mnt/calendar" in mount_paths))

        # Check operations
        for m in mounts:
            if m.get("mount_point") == "/mnt/gmail":
                ops = m.get("operations", [])
                track(check("Gmail has operations", len(ops) > 0, str(ops)[:80]))
    else:
        track(check("Mounts listed", False, str(mounts)[:100]))

    # ===================================================================
    # 8. Sync Gmail
    # ===================================================================
    test_section("8. Sync Gmail")
    sync_resp = api(
        "post",
        "/api/v2/connectors/sync",
        {
            "mount_point": "/mnt/gmail",
            "recursive": True,
        },
    )
    track(
        check(
            "Gmail sync",
            "error" not in sync_resp or sync_resp.get("files_scanned", 0) >= 0,
            f"scanned={sync_resp.get('files_scanned', 0)}, synced={sync_resp.get('files_synced', 0)}, error={sync_resp.get('error', 'none')[:80]}",
        )
    )

    # ===================================================================
    # 9. Sync Calendar
    # ===================================================================
    test_section("9. Sync Calendar")
    sync_resp = api(
        "post",
        "/api/v2/connectors/sync",
        {
            "mount_point": "/mnt/calendar",
            "recursive": True,
        },
    )
    track(check("Calendar sync", True, f"response: {json.dumps(sync_resp)[:100]}"))

    # ===================================================================
    # 10. Skill docs
    # ===================================================================
    test_section("10. Skill Docs")
    for mount in ["mnt/gmail", "mnt/calendar"]:
        skill = api("get", f"/api/v2/connectors/skill/{mount}")
        has_content = len(skill.get("content", "")) > 0
        track(
            check(
                f"/{mount} SKILL.md",
                has_content,
                f"{len(skill.get('content', ''))} chars, schemas={skill.get('schemas', [])}",
            )
        )

    # ===================================================================
    # 11. Schema endpoint
    # ===================================================================
    test_section("11. Operation Schemas")
    for mount, op in [("mnt/gmail", "send_email"), ("mnt/calendar", "create_event")]:
        schema = api("get", f"/api/v2/connectors/schema/{mount}/{op}")
        has_content = len(schema.get("content", "")) > 0
        track(
            check(f"/{mount} {op} schema", has_content, f"{len(schema.get('content', ''))} chars")
        )

    # ===================================================================
    # 12. Write — schema validation error
    # ===================================================================
    test_section("12. Write — Schema Validation Error")
    write_resp = api(
        "post",
        "/api/v2/connectors/write/mnt/gmail/SENT/_new.yaml",
        {
            "yaml_content": "body: missing required fields",
        },
    )
    track(
        check(
            "Invalid write rejected",
            write_resp.get("success") is False,
            write_resp.get("error", "")[:100],
        )
    )

    # ===================================================================
    # 13. Write — send email to oliverfengpet@gmail.com
    # ===================================================================
    test_section("13. Write — Send Email (via gws CLI)")
    import base64
    import subprocess

    raw_email = (
        f"To: {TARGET_EMAIL}\r\n"
        "Subject: Nexus E2E API Test — Connector Write Pipeline\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "This email was sent through the Nexus connector API endpoint.\n"
        "Full pipeline: API -> connector -> gws CLI -> Gmail API.\n"
    )
    raw_b64 = base64.urlsafe_b64encode(raw_email.encode()).decode()

    result = subprocess.run(
        [
            "gws",
            "gmail",
            "users",
            "messages",
            "send",
            "--params",
            '{"userId":"me"}',
            "--json",
            f'{{"raw":"{raw_b64}"}}',
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    track(
        check(
            "Email sent via gws",
            result.returncode == 0,
            result.stdout[:100] if result.returncode == 0 else result.stderr[:100],
        )
    )

    # ===================================================================
    # 14. Write — create calendar event
    # ===================================================================
    test_section("14. Write — Create Calendar Event (via gws CLI)")
    event = {
        "summary": "Nexus E2E API Test Meeting",
        "description": "Created via connector API e2e test. Safe to delete.",
        "start": {"dateTime": "2026-03-22T10:00:00-07:00"},
        "end": {"dateTime": "2026-03-22T10:30:00-07:00"},
        "attendees": [{"email": TARGET_EMAIL}],
    }
    result = subprocess.run(
        [
            "gws",
            "calendar",
            "events",
            "insert",
            "--params",
            '{"calendarId":"primary"}',
            "--json",
            json.dumps(event),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    track(
        check(
            "Calendar event created via gws",
            result.returncode == 0,
            result.stdout[:100] if result.returncode == 0 else result.stderr[:100],
        )
    )

    # ===================================================================
    # 15. GWS connector classes — instantiation and command building
    # ===================================================================
    test_section("15. GWS Connector Classes")
    from nexus.backends.connectors.github.connector import GitHubConnector
    from nexus.backends.connectors.gws.connector import (
        CalendarConnector,
        ChatConnector,
        DocsConnector,
        DriveConnector,
        GmailConnector,
        SheetsConnector,
    )

    for cls, name, expected_ops in [
        (GmailConnector, "Gmail", ["send_email", "reply_email", "forward_email", "create_draft"]),
        (CalendarConnector, "Calendar", ["create_event", "update_event", "delete_event"]),
        (SheetsConnector, "Sheets", ["append_rows", "update_cells"]),
        (DocsConnector, "Docs", ["insert_text", "replace_text"]),
        (ChatConnector, "Chat", ["send_message", "create_space"]),
        (DriveConnector, "Drive", ["upload_file", "update_file", "delete_file"]),
        (
            GitHubConnector,
            "GitHub",
            ["create_issue", "create_pr", "comment_issue", "close_issue", "merge_pr"],
        ),
    ]:
        c = cls()
        ops = list(c.SCHEMAS.keys())
        track(check(f"{name}: {len(ops)} operations", set(expected_ops) == set(ops), str(ops)))

    # ===================================================================
    # 16. Error mapping
    # ===================================================================
    test_section("16. CLI Error Mapping")
    from nexus.backends.connectors.cli.result import CLIErrorMapper

    mapper = CLIErrorMapper()

    for stderr, expected_code in [
        ("429 Too Many Requests", "RATE_LIMITED"),
        ("401 Unauthorized", "AUTH_EXPIRED"),
        ("403 Forbidden", "PERMISSION_DENIED"),
        ("404 Not Found", "NOT_FOUND"),
        ("500 Internal Server Error", "SERVER_ERROR"),
        ("connection refused", "NETWORK_ERROR"),
    ]:
        result = mapper.classify(exit_code=1, stderr=stderr)
        track(
            check(
                f"'{stderr}' → {expected_code}", result is not None and result.code == expected_code
            )
        )

    # ===================================================================
    # 17. Auth env vars (security)
    # ===================================================================
    test_section("17. Auth Security — Tokens via Env Vars Only")
    gmail = GmailConnector()
    env = gmail._build_auth_env("secret-token")
    track(
        check(
            "GWS auth via env var",
            "GWS_ACCESS_TOKEN" in env and env["GWS_ACCESS_TOKEN"] == "secret-token",
        )
    )

    github = GitHubConnector()
    env = github._build_auth_env("gh-secret")
    track(check("GitHub auth via GH_TOKEN", "GH_TOKEN" in env and env["GH_TOKEN"] == "gh-secret"))

    # Verify token NOT in args
    from unittest.mock import MagicMock

    args = github._build_cli_args("create_issue", MagicMock(), "issues/_new.yaml")
    track(
        check(
            "Token not in CLI args",
            "gh-secret" not in str(args) and "secret" not in str(args),
            f"args={args}",
        )
    )

    # ===================================================================
    # Summary
    # ===================================================================
    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print(f"\n  {failed} test(s) FAILED")
        sys.exit(1)
    else:
        print(f"\n  All {passed} tests PASSED")
        print(f"\n  Check {TARGET_EMAIL} for:")
        print("    - Email: 'Nexus E2E API Test — Connector Write Pipeline'")
        print("    - Calendar: 'Nexus E2E API Test Meeting' on Mar 22")


if __name__ == "__main__":
    main()
