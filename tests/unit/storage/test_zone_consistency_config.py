"""Tests for zone consistency configuration — ZoneModel.consistency_mode + enums.

Issue #1180: Consistency Migration
TDD RED phase — tests written before implementation.

Covers:
- ZoneModel.consistency_mode column (default, valid values, CHECK constraint)
- ZoneModel.parsed_settings property (JSON -> ZoneSettings round-trip)
- ConsistencyMode and StoreMode enums
- COMPATIBILITY_MATRIX completeness and specific entries
- Orthogonality of FSConsistency vs ConsistencyMode
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.consistency import (
    COMPATIBILITY_MATRIX,
    DEFAULT_CONSISTENCY,
    DEFAULT_CONSISTENCY_MODE,
    ConsistencyMode,
    FSConsistency,
    StoreMode,
)
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base
from nexus.storage.zone_settings import ZoneSettings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with FK enforcement."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    """Create a database session for testing."""
    session_cls = sessionmaker(bind=engine)
    sess = session_cls()
    yield sess
    sess.close()


def _make_zone(
    zone_id: str = "zone-1",
    name: str = "Test Zone",
    *,
    consistency_mode: str | None = None,
    settings: str | None = None,
) -> ZoneModel:
    """Create a ZoneModel with optional overrides."""
    kwargs: dict = {
        "zone_id": zone_id,
        "name": name,
    }
    if consistency_mode is not None:
        kwargs["consistency_mode"] = consistency_mode
    if settings is not None:
        kwargs["settings"] = settings
    return ZoneModel(**kwargs)


# ===========================================================================
# ZoneModel column tests
# ===========================================================================


class TestZoneModelConsistencyMode:
    """Tests for ZoneModel.consistency_mode column behavior."""

    def test_zone_model_default_consistency_mode(self, session):
        """ZoneModel.consistency_mode defaults to 'SC' when not explicitly set."""
        zone = _make_zone()
        session.add(zone)
        session.commit()

        session.refresh(zone)
        assert zone.consistency_mode == "SC"

    def test_zone_model_ec_mode(self, session):
        """ZoneModel accepts 'EC' as a valid consistency_mode value."""
        zone = _make_zone(zone_id="zone-ec", consistency_mode="EC")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        assert zone.consistency_mode == "EC"

    def test_zone_model_invalid_mode_rejected(self, session):
        """CHECK constraint rejects values outside {'SC', 'EC'}."""
        zone = _make_zone(zone_id="zone-bad", consistency_mode="INVALID")
        session.add(zone)

        with pytest.raises(IntegrityError, match="ck_zones_consistency_mode|CHECK"):
            session.commit()

    def test_zone_model_repr_includes_mode(self, session):
        """ZoneModel.__repr__ includes the consistency_mode field."""
        zone = _make_zone(consistency_mode="EC")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        repr_str = repr(zone)

        assert "consistency_mode" in repr_str
        assert "EC" in repr_str


# ===========================================================================
# ZoneModel.parsed_settings tests
# ===========================================================================


class TestZoneModelParsedSettings:
    """Tests for ZoneModel.parsed_settings property and ZoneSettings."""

    def test_zone_settings_empty(self, session):
        """parsed_settings returns empty ZoneSettings when settings is None."""
        zone = _make_zone(settings=None)
        session.add(zone)
        session.commit()

        session.refresh(zone)
        result = zone.parsed_settings
        assert isinstance(result, ZoneSettings)
        # Empty settings should produce a model with no extra fields
        assert result.model_dump() == {}

    def test_zone_settings_valid_json(self, session):
        """parsed_settings correctly parses valid JSON settings."""
        payload = {"max_file_size": 1024, "retention_days": 30}
        zone = _make_zone(settings=json.dumps(payload))
        session.add(zone)
        session.commit()

        session.refresh(zone)
        result = zone.parsed_settings
        assert isinstance(result, ZoneSettings)
        assert result.max_file_size == 1024
        assert result.retention_days == 30

    def test_zone_settings_extra_fields_allowed(self, session):
        """Unknown fields are preserved via Pydantic extra='allow'."""
        payload = {"custom_flag": True, "tier": "enterprise"}
        zone = _make_zone(settings=json.dumps(payload))
        session.add(zone)
        session.commit()

        session.refresh(zone)
        result = zone.parsed_settings
        assert result.custom_flag is True
        assert result.tier == "enterprise"

    def test_zone_settings_invalid_json(self, session):
        """parsed_settings raises an error for malformed JSON."""
        zone = _make_zone(settings="not-valid-json{{{")
        session.add(zone)
        session.commit()

        session.refresh(zone)
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _ = zone.parsed_settings

    def test_zone_model_settings_roundtrip(self, session):
        """Write JSON settings -> read -> parsed_settings matches original dict."""
        original = {
            "max_file_size": 2048,
            "allowed_types": ["text/plain", "image/png"],
            "nested": {"key": "value"},
        }
        zone = _make_zone(settings=json.dumps(original))
        session.add(zone)
        session.commit()

        session.refresh(zone)
        result = zone.parsed_settings
        dumped = result.model_dump()

        assert dumped["max_file_size"] == 2048
        assert dumped["allowed_types"] == ["text/plain", "image/png"]
        assert dumped["nested"] == {"key": "value"}


# ===========================================================================
# ZoneSettings Pydantic model
# ===========================================================================


class TestZoneSettingsModel:
    """Tests for the ZoneSettings Pydantic model itself."""

    def test_zone_settings_model_config_extra_allow(self):
        """ZoneSettings model_config has extra='allow' for forward compatibility."""
        assert ZoneSettings.model_config.get("extra") == "allow"

    def test_zone_settings_from_empty_dict(self):
        """ZoneSettings can be instantiated with an empty dict."""
        settings = ZoneSettings()
        assert settings.model_dump() == {}

    def test_zone_settings_arbitrary_keys(self):
        """ZoneSettings accepts arbitrary key-value pairs."""
        settings = ZoneSettings(foo="bar", count=42)
        assert settings.foo == "bar"
        assert settings.count == 42


# ===========================================================================
# ConsistencyMode enum tests
# ===========================================================================


class TestConsistencyModeEnum:
    """Tests for the ConsistencyMode StrEnum."""

    def test_consistency_mode_enum_values(self):
        """ConsistencyMode has exactly SC and EC members."""
        members = set(ConsistencyMode)
        assert members == {ConsistencyMode.SC, ConsistencyMode.EC}

    def test_consistency_mode_str_values(self):
        """ConsistencyMode members have correct string values."""
        assert str(ConsistencyMode.SC) == "SC"
        assert str(ConsistencyMode.EC) == "EC"

    def test_default_consistency_mode_is_sc(self):
        """DEFAULT_CONSISTENCY_MODE is SC (strong consistency)."""
        assert DEFAULT_CONSISTENCY_MODE == ConsistencyMode.SC
        assert DEFAULT_CONSISTENCY_MODE == "SC"


# ===========================================================================
# StoreMode enum tests
# ===========================================================================


class TestStoreModeEnum:
    """Tests for the StoreMode StrEnum."""

    def test_store_mode_enum_values(self):
        """StoreMode has exactly 4 members: EMBEDDED, SC, EC, REMOTE."""
        members = set(StoreMode)
        assert members == {
            StoreMode.EMBEDDED,
            StoreMode.SC,
            StoreMode.EC,
            StoreMode.REMOTE,
        }

    def test_store_mode_str_values(self):
        """StoreMode members have correct lowercase string values."""
        assert str(StoreMode.EMBEDDED) == "embedded"
        assert str(StoreMode.SC) == "sc"
        assert str(StoreMode.EC) == "ec"
        assert str(StoreMode.REMOTE) == "remote"


# ===========================================================================
# COMPATIBILITY_MATRIX tests
# ===========================================================================


class TestCompatibilityMatrix:
    """Tests for the COMPATIBILITY_MATRIX mapping."""

    def test_compat_matrix_has_all_combinations(self):
        """Matrix covers all 2 ConsistencyModes x 3 FSConsistency = 6 entries."""
        assert len(COMPATIBILITY_MATRIX) == 6

        for mode in ConsistencyMode:
            for fs_level in FSConsistency:
                key = (mode, fs_level)
                assert key in COMPATIBILITY_MATRIX, f"Missing matrix entry for {key}"

    def test_compat_matrix_sc_strong_is_wait_or_raise(self):
        """SC + STRONG = strictest behavior (wait_or_raise)."""
        behavior = COMPATIBILITY_MATRIX[(ConsistencyMode.SC, FSConsistency.STRONG)]
        assert behavior == "wait_or_raise"

    def test_compat_matrix_ec_eventual_is_skip(self):
        """EC + EVENTUAL = most lenient behavior (skip_zookie_wait)."""
        behavior = COMPATIBILITY_MATRIX[(ConsistencyMode.EC, FSConsistency.EVENTUAL)]
        assert behavior == "skip_zookie_wait"

    def test_compat_matrix_ec_strong_is_warn(self):
        """EC + STRONG = warning behavior (warn_then_wait)."""
        behavior = COMPATIBILITY_MATRIX[(ConsistencyMode.EC, FSConsistency.STRONG)]
        assert behavior == "warn_then_wait"

    def test_compat_matrix_values_are_known_behaviors(self):
        """All matrix values are from the known set of behaviors."""
        known_behaviors = {
            "skip_zookie_wait",
            "wait_best_effort",
            "wait_or_raise",
            "warn_then_wait",
        }
        for key, behavior in COMPATIBILITY_MATRIX.items():
            assert behavior in known_behaviors, f"Unknown behavior '{behavior}' for {key}"


# ===========================================================================
# Orthogonality and defaults
# ===========================================================================


class TestOrthogonalityAndDefaults:
    """Tests verifying that FSConsistency and ConsistencyMode are independent."""

    def test_default_consistency_unchanged(self):
        """DEFAULT_CONSISTENCY is still CLOSE_TO_OPEN (Issue #923 contract)."""
        assert DEFAULT_CONSISTENCY == FSConsistency.CLOSE_TO_OPEN

    def test_fs_consistency_orthogonal_to_consistency_mode(self):
        """FSConsistency and ConsistencyMode are different enums with different values.

        FSConsistency controls per-operation read freshness (eventual/close_to_open/strong).
        ConsistencyMode controls per-zone write replication (SC/EC).
        They should share no member names or string values.
        """
        fs_values = {str(v) for v in FSConsistency}
        mode_values = {str(v) for v in ConsistencyMode}

        # No overlap in string values
        assert fs_values.isdisjoint(mode_values), f"Overlap found: {fs_values & mode_values}"

        # Different number of members
        assert len(FSConsistency) == 3
        assert len(ConsistencyMode) == 2

        # Different base purpose (verify they are distinct types)
        assert FSConsistency is not ConsistencyMode
