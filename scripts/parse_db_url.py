#!/usr/bin/env python3
"""
Parse database URL and extract connection components.

Usage:
    python scripts/parse_db_url.py <database_url> <component>

Components:
    host - Database hostname
    port - Database port (default: 5432)
"""

import sys
from urllib.parse import urlparse


def parse_db_url(database_url: str, component: str) -> str:
    """
    Parse database URL and extract component.

    Args:
        database_url: Database connection URL
        component: Component to extract ('host' or 'port')

    Returns:
        Component value or empty string if not found
    """
    try:
        parsed = urlparse(database_url)

        if component == "host":
            return parsed.hostname or ""
        elif component == "port":
            return str(parsed.port) if parsed.port else "5432"
        else:
            return ""
    except Exception:
        return ""


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 3:
        print("Usage: python parse_db_url.py <database_url> <component>", file=sys.stderr)
        print("Components: host, port", file=sys.stderr)
        sys.exit(1)

    database_url = sys.argv[1]
    component = sys.argv[2]

    result = parse_db_url(database_url, component)
    print(result)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
