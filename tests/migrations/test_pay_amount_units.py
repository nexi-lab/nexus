"""Regression coverage for Nexus Pay amount-unit migration."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "alembic" / "versions" / "pay_amounts_micro_units.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("pay_amounts_micro_units", MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pay_amount_migration_converts_legacy_cent_amounts_to_micro(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pay.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE payment_transaction_meta (amount BIGINT NOT NULL)"))
        conn.execute(text("CREATE TABLE credit_reservation_meta (amount BIGINT NOT NULL)"))
        conn.execute(text("INSERT INTO payment_transaction_meta (amount) VALUES (255)"))
        conn.execute(text("INSERT INTO credit_reservation_meta (amount) VALUES (550)"))

        _load_migration().upgrade_pay_amounts(conn)

        tx_amount = conn.execute(text("SELECT amount FROM payment_transaction_meta")).scalar_one()
        reservation_amount = conn.execute(
            text("SELECT amount FROM credit_reservation_meta")
        ).scalar_one()

    assert tx_amount == 2_550_000
    assert reservation_amount == 5_500_000
