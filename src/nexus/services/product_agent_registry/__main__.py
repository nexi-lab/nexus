"""Run once by an operator before enabling the Registry service."""

import argparse
import os

from sqlalchemy import create_engine

from nexus.services.product_agent_registry.store import initialize_schema


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize new product Registry tables; no legacy copy/delete/cutover"
    )
    parser.add_argument("--initialize-schema", action="store_true", required=True)
    parser.parse_args()
    engine = create_engine(os.environ["NEXUS_AGENT_REGISTRY_DATABASE_URL"])
    try:
        initialize_schema(engine)
    finally:
        engine.dispose()
    print("Product Registry tables initialized. No legacy records were migrated.")


if __name__ == "__main__":
    main()
