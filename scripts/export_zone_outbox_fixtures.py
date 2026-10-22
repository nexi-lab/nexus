"""Freeze the current SQL outbox schema for cross-language compatibility tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from nexus.storage.models import ZoneGrantProjectionOutboxModel, ZoneRuntimeOutboxModel

DESTINATION = (
    Path(__file__).resolve().parents[1]
    / "rust"
    / "services"
    / "zone-control"
    / "tests"
    / "fixtures"
)


def fixtures() -> dict[str, str]:
    result = {}
    for name, dialect in (("sqlite", sqlite.dialect()), ("postgres", postgresql.dialect())):
        statements = []
        for model in (ZoneRuntimeOutboxModel, ZoneGrantProjectionOutboxModel):
            statements.append(str(CreateTable(model.__table__).compile(dialect=dialect)).strip())
            statements.extend(
                str(CreateIndex(index).compile(dialect=dialect)).strip()
                for index in sorted(model.__table__.indexes, key=lambda index: index.name)
            )
        result[f"zone-outboxes.{name}.sql"] = (
            "\n".join(line.rstrip() for line in (";\n\n".join(statements) + ";").splitlines())
            + "\n"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in fixtures().items():
        path = DESTINATION / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf8") != content:
                raise SystemExit(f"Outbox SQL compatibility fixture differs from its model: {name}")
        else:
            DESTINATION.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf8", newline="\n")


if __name__ == "__main__":
    main()
