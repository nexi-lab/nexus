"""Unit tests for readable_zone_filter / token_zone_filter_from_auth (#4557).

Covers the three verified gaps in the #4542/#4558 read-attenuation logic:

  Gap B: a malformed zone_perms entry (``None``, a non-list/tuple, wrong
      length, or a non-str member) must neither crash ``readable_zone_filter``
      nor stringify into a false-positive READABLE zone (e.g. ``str(True)``
      contains "r").
  Gap C: an explicit non-read root grant (``root_zone_id`` with zone_perms
      that only ever grant write) must not be treated as unbounded access —
      ``token_zone_filter_from_auth`` must fail closed (empty frozenset), not
      return ``None``.
"""

from __future__ import annotations

from nexus.bricks.search.search_auth import (
    readable_zone_filter,
    token_zone_filter_from_auth,
)

ROOT = "root"


class TestReadableZoneFilterMalformedEntries:
    def test_mixed_malformed_and_valid_entries_no_exception(self) -> None:
        """A None entry must not raise TypeError from len(None); a non-str
        perms member (True) must not stringify into a false-positive read
        grant; only the one well-formed, readable entry survives."""
        zone_perms = [
            None,
            "junk",
            ["eng", "r"],
            ["ops", True],
            [1, "r"],
            ["a", "b", "c"],
        ]
        result = readable_zone_filter(["eng", "ops"], zone_perms)
        assert result == frozenset({"eng"})

    def test_all_malformed_entries_fail_closed_not_zone_set_fallback(self) -> None:
        """zone_perms present but every entry malformed -> empty frozenset
        (fail closed), NOT a fall-through to the raw zone_set."""
        result = readable_zone_filter(["eng", "ops"], [None, ["x"]])
        assert result == frozenset()
        assert result is not None

    def test_empty_zone_perms_falls_back_to_zone_set(self) -> None:
        """Falsy zone_perms (legacy tokens) keep the whole zone_set."""
        result = readable_zone_filter(["eng", "ops"], [])
        assert result == frozenset({"eng", "ops"})

    def test_no_zone_set_no_zone_perms_returns_none(self) -> None:
        assert readable_zone_filter([], None) is None

    def test_well_formed_write_only_entry_excluded(self) -> None:
        result = readable_zone_filter(["eng", "legal"], [["eng", "r"], ["legal", "w"]])
        assert result == frozenset({"eng"})


class TestTokenZoneFilterFromAuthRootGrant:
    def test_root_write_only_grant_fails_closed(self) -> None:
        auth = {"zone_set": ["root"], "zone_perms": [["root", "w"]]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) == frozenset()

    def test_root_read_grant_stays_unbounded(self) -> None:
        auth = {"zone_set": ["root"], "zone_perms": [["root", "r"]]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_root_execute_grant_stays_unbounded(self) -> None:
        auth = {"zone_set": ["root"], "zone_perms": [["root", "x"]]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_duplicate_root_entries_aggregate_order_independently_forward(self) -> None:
        auth = {
            "zone_set": ["root"],
            "zone_perms": [["root", "w"], ["root", "rx"]],
        }
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_duplicate_root_entries_aggregate_order_independently_reversed(self) -> None:
        auth = {
            "zone_set": ["root"],
            "zone_perms": [["root", "rx"], ["root", "w"]],
        }
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_root_zone_set_no_zone_perms_legacy_unchanged(self) -> None:
        """A root-only zone_set with NO zone_perms at all (legacy tokens
        predate per-zone perms) keeps the pre-#4557 exemption."""
        auth = {"zone_set": ["root"]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_root_zone_set_empty_zone_perms_legacy_unchanged(self) -> None:
        auth = {"zone_set": ["root"], "zone_perms": []}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_admin_with_root_write_only_stays_unbounded(self) -> None:
        auth = {
            "zone_set": ["root"],
            "zone_perms": [["root", "w"]],
            "is_admin": True,
        }
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_malformed_root_entries_treated_as_absent_legacy_unchanged(self) -> None:
        """Malformed root entries don't count as an "explicit" grant --
        same well-formed test as readable_zone_filter."""
        auth = {"zone_set": ["root"], "zone_perms": [None, ["root"], [1, "w"]]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) is None

    def test_unconstrained_credential_stays_unbounded(self) -> None:
        assert token_zone_filter_from_auth({"zone_set": []}, root_zone_id=ROOT) is None

    def test_non_root_zone_set_uses_readable_zone_filter(self) -> None:
        auth = {"zone_set": ["eng"], "zone_perms": [["eng", "w"]]}
        assert token_zone_filter_from_auth(auth, root_zone_id=ROOT) == frozenset()
