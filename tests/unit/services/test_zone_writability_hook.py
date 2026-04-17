"""Tests for ZoneWritabilityHook — PRE hook gating writes during zone deprovision."""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.lifecycle.zone_writability_hook import ZoneWritabilityHook


@pytest.fixture
def zone_lifecycle():
    zl = MagicMock()
    zl.is_zone_terminating.return_value = False
    return zl


@pytest.fixture
def hook(zone_lifecycle):
    return ZoneWritabilityHook(zone_lifecycle)


class TestZoneWritabilityHook:
    def test_hook_spec_declares_all_mutating_ops(self, hook):
        spec = hook.hook_spec()
        assert len(spec.write_hooks) == 1
        assert len(spec.delete_hooks) == 1
        assert len(spec.rename_hooks) == 1
        assert len(spec.mkdir_hooks) == 1
        assert len(spec.rmdir_hooks) == 1
        assert spec.read_hooks == ()

    def test_pre_write_allows_normal_zone(self, hook):
        ctx = MagicMock(zone_id=ROOT_ZONE_ID)
        hook.on_pre_write(ctx)  # should not raise

    def test_pre_write_blocks_terminating_zone(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="doomed-zone")
        with pytest.raises(Exception, match="doomed-zone"):
            hook.on_pre_write(ctx)

    def test_pre_delete_blocks_terminating_zone(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="doomed-zone")
        with pytest.raises(Exception, match="doomed-zone"):
            hook.on_pre_delete(ctx)

    def test_pre_mkdir_blocks_terminating_zone(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="doomed-zone")
        with pytest.raises(Exception, match="doomed-zone"):
            hook.on_pre_mkdir(ctx)

    def test_pre_rename_blocks_terminating_zone(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="doomed-zone")
        with pytest.raises(Exception, match="doomed-zone"):
            hook.on_pre_rename(ctx)

    def test_pre_rmdir_blocks_terminating_zone(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="doomed-zone")
        with pytest.raises(Exception, match="doomed-zone"):
            hook.on_pre_rmdir(ctx)

    def test_none_zone_id_is_noop(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id=None)
        hook.on_pre_write(ctx)  # should not raise — None zone_id is no-op

    def test_empty_zone_id_is_noop(self, hook, zone_lifecycle):
        zone_lifecycle.is_zone_terminating.return_value = True
        ctx = MagicMock(zone_id="")
        hook.on_pre_write(ctx)  # should not raise — empty zone_id is no-op
