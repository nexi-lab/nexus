"""``sys_readdir`` ReBAC post-filter for non-admin callers (Issue #4739).

``sys_readdir`` applied zone filtering only, so a non-admin key could list the
names of files it cannot read (the HTTP ``/files/list`` route calls it
directly).  ``nexus.core.readdir_rebac_filter.readdir_visible_paths`` now runs
the same enforcer chain the search service uses; admin / system /
identity-less callers keep the unfiltered view.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.readdir_rebac_filter import readdir_filter_context, readdir_visible_paths

ENTRIES: list[tuple[str, bool]] = [
    ("/ws/a.txt", False),
    ("/ws/b.txt", False),
    ("/ws/sub", True),
    ("/ws/other", True),
]


def _enforcer(allowed_files: set[str], visible_dirs: set[str]) -> MagicMock:
    enf = MagicMock(name="permission_enforcer")
    enf.filter_list.side_effect = lambda paths, ctx: [p for p in paths if p in allowed_files]
    enf.has_accessible_descendants_batch.side_effect = lambda dirs, ctx: {
        d: d in visible_dirs for d in dirs
    }
    return enf


def _fs(enforcer: Any = None, *, enforce: bool = True, perm: Any = "default") -> SimpleNamespace:
    """Just enough NexusFS surface for the filter: config + service lookup."""
    if perm == "default":
        perm = PermissionConfig(enforce=enforce)
    return SimpleNamespace(
        _perm_config=perm,
        service=lambda name: enforcer if name == "permission_enforcer" else None,
    )


BOB = OperationContext(user_id="bob", groups=[], zone_id="root")
ADMIN = OperationContext(user_id="admin", groups=[], zone_id="root", is_admin=True)
SYSTEM = OperationContext(user_id="system", groups=[], zone_id="root", is_system=True)


class TestNonAdminFiltering:
    def test_files_through_filter_list_and_dirs_through_descendants(self):
        enf = _enforcer(allowed_files={"/ws/a.txt"}, visible_dirs={"/ws/sub"})

        visible = readdir_visible_paths(_fs(enf), ENTRIES, BOB)

        assert visible == {"/ws/a.txt", "/ws/sub"}
        files_arg, ctx_arg = enf.filter_list.call_args.args
        assert files_arg == ["/ws/a.txt", "/ws/b.txt"]
        assert ctx_arg is BOB
        dirs_arg, _ = enf.has_accessible_descendants_batch.call_args.args
        assert dirs_arg == ["/ws/sub", "/ws/other"]

    def test_directory_without_answer_stays_visible(self):
        """Matches SearchService: dict.get(prefix, True) for indeterminate dirs."""
        enf = _enforcer(allowed_files=set(), visible_dirs=set())
        enf.has_accessible_descendants_batch.side_effect = lambda dirs, ctx: {}

        assert readdir_visible_paths(_fs(enf), ENTRIES, BOB) == {"/ws/sub", "/ws/other"}

    def test_no_files_skips_filter_list(self):
        enf = _enforcer(allowed_files=set(), visible_dirs={"/ws/sub"})

        assert readdir_visible_paths(_fs(enf), [("/ws/sub", True)], BOB) == {"/ws/sub"}
        enf.filter_list.assert_not_called()

    def test_strong_consistency_context_reaches_enforcer(self):
        enf = _enforcer(allowed_files=set(), visible_dirs=set())
        strong = OperationContext(user_id="bob", groups=[], zone_id="root", consistency="strong")

        readdir_visible_paths(_fs(enf), ENTRIES, strong)

        assert enf.filter_list.call_args.args[1].consistency == "strong"

    def test_empty_entries_do_not_filter(self):
        enf = _enforcer(allowed_files=set(), visible_dirs=set())
        assert readdir_visible_paths(_fs(enf), [], BOB) is None
        enf.filter_list.assert_not_called()


class TestUnchangedCallers:
    @pytest.mark.parametrize("ctx", [None, ADMIN, SYSTEM], ids=["no-identity", "admin", "system"])
    def test_admin_system_and_identity_less_are_not_filtered(self, ctx):
        enf = _enforcer(allowed_files=set(), visible_dirs=set())

        assert readdir_visible_paths(_fs(enf), ENTRIES, ctx) is None
        enf.filter_list.assert_not_called()

    def test_enforcement_disabled_is_not_filtered(self):
        enf = _enforcer(allowed_files=set(), visible_dirs=set())
        assert readdir_visible_paths(_fs(enf, enforce=False), ENTRIES, BOB) is None

    def test_missing_permission_config_is_not_filtered(self):
        enf = _enforcer(allowed_files=set(), visible_dirs=set())
        assert readdir_visible_paths(_fs(enf, perm=None), ENTRIES, BOB) is None


class TestFailClosed:
    def test_enforcer_unavailable_hides_everything(self):
        assert readdir_visible_paths(_fs(None), ENTRIES, BOB) == set()

    def test_object_without_service_lookup_hides_everything(self):
        fs = SimpleNamespace(_perm_config=PermissionConfig(enforce=True))
        assert readdir_visible_paths(fs, ENTRIES, BOB) == set()


class TestDictContexts:
    def test_dict_with_subject_is_promoted_and_filtered(self):
        enf = _enforcer(allowed_files={"/ws/a.txt"}, visible_dirs=set())
        ctx = {"user_id": "bob", "zone_id": "root", "consistency": "strong"}

        visible = readdir_visible_paths(_fs(enf), ENTRIES, ctx)

        assert visible == {"/ws/a.txt"}
        promoted = enf.filter_list.call_args.args[1]
        assert isinstance(promoted, OperationContext)
        assert promoted.user_id == "bob"
        assert promoted.zone_id == "root"
        assert promoted.consistency == "strong"

    def test_dict_admin_or_system_is_not_filtered(self):
        assert readdir_filter_context({"user_id": "a", "is_admin": True}) is None
        assert readdir_filter_context({"user_id": "s", "is_system": True}) is None

    def test_dict_without_subject_is_not_filtered(self):
        assert readdir_filter_context({"zone_id": "root"}) is None

    def test_operation_context_passthrough(self):
        assert readdir_filter_context(BOB) is BOB
        assert readdir_filter_context(ADMIN) is None
        assert readdir_filter_context(object()) is None
