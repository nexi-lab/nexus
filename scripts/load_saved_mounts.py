#!/usr/bin/env python3
"""
Load saved mounts from database via Nexus API.

Uses stdlib urllib only (no requests dependency).

Usage:
    python scripts/load_saved_mounts.py <nexus_url> <admin_api_key>
"""

import json
import sys
import urllib.error
import urllib.request


def load_saved_mounts(nexus_url: str, admin_api_key: str) -> bool:
    """
    Load all saved mounts from database.

    Args:
        nexus_url: Base URL of Nexus server (e.g., http://localhost:2026)
        admin_api_key: Admin API key for authentication

    Returns:
        True if successful, False otherwise
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_api_key}",
        }

        # List saved mounts
        list_url = f"{nexus_url.rstrip('/')}/api/nfs/list_saved_mounts"
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "list_saved_mounts"}).encode(
            "utf-8"
        )

        req = urllib.request.Request(list_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        mounts = data.get("result", [])

        if not mounts:
            print("No saved mounts found")
            return True

        print(f"Found {len(mounts)} saved mount(s)")

        # Load each mount
        load_url = f"{nexus_url.rstrip('/')}/api/nfs/load_mount"
        success_count = 0

        for mount in mounts:
            mount_point = mount.get("mount_point")
            if not mount_point:
                continue

            print(f"  Loading mount: {mount_point}")

            load_payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "load_mount",
                    "params": {"mount_point": mount_point},
                }
            ).encode("utf-8")

            try:
                load_req = urllib.request.Request(
                    load_url, data=load_payload, headers=headers, method="POST"
                )
                with urllib.request.urlopen(load_req, timeout=10) as load_resp:
                    load_resp.read()
                print(f"    ✓ Loaded: {mount_point}")
                success_count += 1
            except Exception as e:
                print(f"    ⚠ Failed to load: {mount_point} - {e}")

        return success_count > 0 or len(mounts) == 0

    except Exception as e:
        print(f"ERROR: Failed to load saved mounts: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return False


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 3:
        print("Usage: python load_saved_mounts.py <nexus_url> <admin_api_key>", file=sys.stderr)
        sys.exit(1)

    nexus_url = sys.argv[1]
    admin_api_key = sys.argv[2]

    if not nexus_url or not admin_api_key:
        print("ERROR: nexus_url and admin_api_key are required", file=sys.stderr)
        sys.exit(1)

    success = load_saved_mounts(nexus_url, admin_api_key)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
