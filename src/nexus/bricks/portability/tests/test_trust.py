"""Tests for TOFU trust store."""

import json

from nexus.bricks.portability.trust import TrustStore


def test_first_see_returns_unseen(tmp_path):
    store = TrustStore(tmp_path / "trusted_signers.json")
    assert store.is_trusted("pubkey1") is False


def test_pin_then_trusted(tmp_path):
    store = TrustStore(tmp_path / "trusted_signers.json")
    store.pin("pubkey1", label="alice@hub")
    assert store.is_trusted("pubkey1") is True


def test_pin_persists_across_instances(tmp_path):
    path = tmp_path / "trusted_signers.json"
    s1 = TrustStore(path)
    s1.pin("pubkey1", label="alice@hub")
    s2 = TrustStore(path)
    assert s2.is_trusted("pubkey1") is True


def test_pin_records_first_seen(tmp_path):
    path = tmp_path / "trusted_signers.json"
    store = TrustStore(path)
    store.pin("pubkey1", label="alice@hub")
    raw = json.loads(path.read_text())
    assert "first_seen" in raw["pubkey1"]
    assert raw["pubkey1"]["label"] == "alice@hub"


def test_corrupted_file_returns_empty(tmp_path):
    path = tmp_path / "trusted_signers.json"
    path.write_text("not json")
    store = TrustStore(path)
    assert store.is_trusted("anything") is False
