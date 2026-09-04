"""Unit tests for ``nexus.lib.zone_visibility`` (#4740).

The ZoneView is the single predicate behind ``sys_readdir`` and the search
list/glob/grep pipeline.  These tests pin the fail-closed rules:

* zone-less non-admin callers are refused, not resolved to ROOT;
* root-tagged entries are visible only to callers that can read ROOT;
* admins cross zones only via an explicit ``all_zones=True`` (audited);
* ``context=None`` (kernel / internal) and zone-less ``is_system`` stay
  unrestricted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import PermissionDeniedError
from nexus.contracts.types import OperationContext
from nexus.lib import zone_visibility as zv
from nexus.lib.events import register_audit_sink
from nexus.lib.zone_visibility import (
    ALL_ZONES_AUDIT_EVENT,
    ZoneView,
    audit_all_zones,
    readable_zones_from_perms,
    resolve_zone_view,
    zone_from_path,
)


def _ctx(**kwargs) -> OperationContext:
    kwargs.setdefault("user_id", "alice")
    kwargs.setdefault("groups", [])
    return OperationContext(**kwargs)


class TestZoneFromPath:
    def test_extracts_embedded_zone(self) -> None:
        assert zone_from_path("/zone/acme/docs/a.txt") == "acme"

    def test_zone_dir_itself(self) -> None:
        assert zone_from_path("/zone/acme") == "acme"

    def test_legacy_zones_prefix_matches_rebac_chain(self) -> None:
        assert zone_from_path("/zones/acme/docs/a.txt") == "acme"

    def test_non_zone_paths_and_junk(self) -> None:
        assert zone_from_path("/docs/a.txt") is None
        assert zone_from_path("/workspace/acme/a.txt") is None  # legacy layout, not a zone ns
        assert zone_from_path("/zone/") is None
        assert zone_from_path(None) is None
        assert zone_from_path(42) is None


class TestReadableZonesFromPerms:
    def test_read_or_execute_grants_pass(self) -> None:
        assert readable_zones_from_perms((("a", "r"), ("b", "x"), ("c", "w"))) == {"a", "b"}

    def test_root_grant_counts_when_readable(self) -> None:
        assert readable_zones_from_perms(((ROOT_ZONE_ID, "rw"),)) == {ROOT_ZONE_ID}

    def test_no_perms_is_none(self) -> None:
        assert readable_zones_from_perms(()) is None
        assert readable_zones_from_perms(None) is None

    def test_all_malformed_is_none_but_write_only_is_empty(self) -> None:
        assert readable_zones_from_perms([None, "junk", ("a",), (1, "r")]) is None
        assert readable_zones_from_perms((("a", "w"),)) == frozenset()


class TestZoneViewAllows:
    def test_unrestricted_allows_everything(self) -> None:
        view = ZoneView(zones=None)
        assert view.unrestricted
        assert view.allows(None, "/anything")
        assert view.allows("other", "/zone/other/x")

    def test_path_zone_is_authoritative(self) -> None:
        view = ZoneView(zones=frozenset({"ta"}))
        # root-tagged entry written inside the tenant namespace is the tenant's
        assert view.allows(None, "/zone/ta/rooty.txt")
        assert view.allows("ta", "/zone/ta/a.txt")
        assert not view.allows("ta", "/zone/tb/leak.txt")

    def test_column_zone_used_outside_zone_namespace(self) -> None:
        view = ZoneView(zones=frozenset({"ta"}))
        assert view.allows("ta", "/flat.txt")  # standalone flat-stored tenant row
        assert not view.allows(None, "/root.txt")  # root-tagged: hidden from tenants
        assert not view.allows(ROOT_ZONE_ID, "/root.txt")

    def test_root_view_sees_root_tagged_only(self) -> None:
        view = ZoneView(zones=frozenset({ROOT_ZONE_ID}))
        assert view.allows(None, "/root.txt")
        assert view.allows("", "/root.txt")
        assert not view.allows("ta", "/flat.txt")
        assert not view.allows(None, "/zone/ta/rooty.txt")


class TestResolveZoneView:
    def test_none_context_is_kernel_unrestricted(self) -> None:
        assert resolve_zone_view(None).unrestricted

    def test_zone_less_non_admin_is_refused(self) -> None:
        with pytest.raises(PermissionDeniedError, match="no zone claim"):
            resolve_zone_view(_ctx())

    def test_zone_less_non_admin_dict_is_refused(self) -> None:
        with pytest.raises(PermissionDeniedError):
            resolve_zone_view({"is_admin": False, "user_id": "alice"})

    def test_zone_less_admin_gets_root_zone_not_global(self) -> None:
        view = resolve_zone_view(_ctx(is_admin=True))
        assert view.zones == {ROOT_ZONE_ID}
        assert not view.unrestricted

    def test_zone_less_system_is_unrestricted(self) -> None:
        assert resolve_zone_view(_ctx(user_id="system", is_system=True)).unrestricted

    def test_system_with_zone_is_scoped_to_it_but_keeps_root_namespace(self) -> None:
        view = resolve_zone_view(_ctx(user_id="system", is_system=True, zone_id="ta"))
        assert view.zones == {"ta"}
        assert view.system
        # internal scans (ReBAC expansion, zone export) still see the
        # root-namespace rows a standalone kernel stamps on every write …
        assert view.allows(None, "/workspace/ta/x.txt")
        assert view.allows(ROOT_ZONE_ID, "/root.txt")
        # … but not another zone's namespace or another zone's flat rows.
        assert not view.allows(None, "/zone/tb/x.txt")
        assert not view.allows("tb", "/flat-tb.txt")

    def test_user_with_zone_does_not_get_root_namespace(self) -> None:
        view = resolve_zone_view(_ctx(zone_id="ta"))
        assert not view.system
        assert not view.allows(None, "/workspace/ta/x.txt")

    def test_single_zone_context(self) -> None:
        assert resolve_zone_view(_ctx(zone_id="ta")).zones == {"ta"}

    def test_explicit_root_zone_context(self) -> None:
        assert resolve_zone_view(_ctx(zone_id=ROOT_ZONE_ID)).zones == {ROOT_ZONE_ID}

    def test_admin_in_a_zone_sees_that_zone_only(self) -> None:
        assert resolve_zone_view(_ctx(zone_id="ta", is_admin=True)).zones == {"ta"}

    def test_multi_zone_token_root_placeholder_does_not_grant_root(self) -> None:
        ctx = _ctx(zone_id=ROOT_ZONE_ID, zone_perms=(("ta", "r"), ("tb", "rw")))
        assert resolve_zone_view(ctx).zones == {"ta", "tb"}

    def test_multi_zone_token_with_root_read_grant_sees_root(self) -> None:
        ctx = _ctx(zone_id=ROOT_ZONE_ID, zone_perms=(("ta", "r"), (ROOT_ZONE_ID, "r")))
        assert resolve_zone_view(ctx).zones == {"ta", ROOT_ZONE_ID}

    def test_write_only_grants_see_nothing(self) -> None:
        ctx = _ctx(zone_id="ta", zone_perms=(("ta", "w"),))
        view = resolve_zone_view(ctx)
        assert view.zones == frozenset()
        assert not view.allows("ta", "/zone/ta/a.txt")

    def test_legacy_zone_set_only_dict_context(self) -> None:
        view = resolve_zone_view({"zone_set": ["ta", "tb"], "is_admin": False})
        assert view.zones == {"ta", "tb"}

    def test_all_zones_requires_admin(self) -> None:
        with pytest.raises(PermissionDeniedError, match="all_zones"):
            resolve_zone_view(_ctx(zone_id="ta"), all_zones=True)

    def test_all_zones_admin_is_unrestricted_and_flagged(self) -> None:
        view = resolve_zone_view(_ctx(is_admin=True), all_zones=True)
        assert view.unrestricted
        assert view.all_zones

    def test_simple_namespace_context(self) -> None:
        ctx = SimpleNamespace(zone_id="ta", is_admin=False, zone_perms=(), zone_set=())
        assert resolve_zone_view(ctx).zones == {"ta"}


class TestAuditAllZones:
    def test_emits_once_per_request(self) -> None:
        seen: list[tuple[str, dict]] = []
        handle = register_audit_sink(lambda name, payload: seen.append((name, payload)))
        try:
            ctx = _ctx(user_id="root-op", is_admin=True, zone_id=ROOT_ZONE_ID)
            audit_all_zones(ctx, operation="list", path="/")
            audit_all_zones(ctx, operation="list", path="/zone/ta")  # nested call, same request
            audit_all_zones(ctx, operation="search.list", path="/")  # different operation
        finally:
            handle.remove()
        names = [n for n, _ in seen]
        assert names == [ALL_ZONES_AUDIT_EVENT, ALL_ZONES_AUDIT_EVENT]
        ops = sorted(p["operation"] for _, p in seen)
        assert ops == ["list", "search.list"]
        payload = seen[0][1]
        assert payload["subject_id"] == "root-op"
        assert payload["zone_id"] == ROOT_ZONE_ID
        assert payload["request_id"] == ctx.request_id

    def test_contexts_without_request_id_always_emit(self) -> None:
        seen: list[str] = []
        handle = register_audit_sink(lambda name, _payload: seen.append(name))
        try:
            audit_all_zones({"is_admin": True}, operation="list", path="/")
            audit_all_zones({"is_admin": True}, operation="list", path="/")
        finally:
            handle.remove()
        assert seen == [ALL_ZONES_AUDIT_EVENT, ALL_ZONES_AUDIT_EVENT]

    def test_recent_audit_cache_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(zv, "_RECENT_AUDITS_MAX", 3)
        zv._RECENT_AUDITS.clear()
        for i in range(10):
            assert zv._already_audited(f"req-{i}", "list") is False
        assert len(zv._RECENT_AUDITS) == 3
        # evicted keys are treated as new again
        assert zv._already_audited("req-0", "list") is False


class TestProcessCredential:
    """The kernel's own init credential is the embedded operator, not a zone-less tenant.

    ``create_nexus_fs`` installs ``OperationContext(user_id="system")`` — no
    zone, not admin — as ``NexusFS._init_cred``; the MCP tools pass it
    explicitly in sandbox mode.  Refusing it broke
    ``test_issue_4129_sandbox_search_e2e`` in CI.
    """

    def test_init_cred_passed_explicitly_is_unrestricted(self) -> None:
        cred = _ctx(user_id="system")
        assert resolve_zone_view(cred, init_cred=cred).unrestricted is True

    def test_identity_not_equality(self) -> None:
        import dataclasses

        cred = _ctx(user_id="system")
        twin = dataclasses.replace(cred)
        assert twin is not cred
        with pytest.raises(PermissionDeniedError):
            resolve_zone_view(twin, init_cred=cred)

    def test_same_context_without_init_cred_is_refused(self) -> None:
        cred = _ctx(user_id="system")
        with pytest.raises(PermissionDeniedError):
            resolve_zone_view(cred)

    def test_init_cred_matches_context_none_even_with_a_zone(self) -> None:
        # ``context=None`` resolves to the init credential and is unrestricted;
        # passing the credential explicitly must not narrow that.
        cred = _ctx(user_id="system", zone_id="acme")
        assert resolve_zone_view(cred, init_cred=cred).unrestricted is True

    def test_all_zones_from_init_cred_is_not_refused(self) -> None:
        cred = _ctx(user_id="system")
        assert resolve_zone_view(cred, all_zones=True, init_cred=cred).unrestricted is True
