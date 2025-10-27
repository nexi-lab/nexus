"""Tests for workflow triggers."""

import pytest

from nexus.workflows.triggers import (
    BUILTIN_TRIGGERS,
    FileDeleteTrigger,
    FileRenameTrigger,
    FileWriteTrigger,
    ManualTrigger,
    MetadataChangeTrigger,
    ScheduleTrigger,
    TriggerManager,
    WebhookTrigger,
)
from nexus.workflows.types import TriggerType


class TestFileWriteTrigger:
    """Test FileWriteTrigger."""

    def test_matches_pattern(self):
        """Test file write trigger matches pattern."""
        trigger = FileWriteTrigger(config={"pattern": "*.md"})
        assert trigger.matches({"file_path": "/docs/readme.md"})
        assert not trigger.matches({"file_path": "/docs/readme.txt"})

    def test_matches_wildcard(self):
        """Test file write trigger with wildcard pattern."""
        trigger = FileWriteTrigger(config={"pattern": "*"})
        assert trigger.matches({"file_path": "/any/file.txt"})

    def test_matches_directory_pattern(self):
        """Test file write trigger with directory pattern."""
        trigger = FileWriteTrigger(config={"pattern": "/docs/*.md"})
        assert trigger.matches({"file_path": "/docs/readme.md"})
        assert not trigger.matches({"file_path": "/other/readme.md"})

    def test_get_pattern(self):
        """Test getting pattern from trigger."""
        trigger = FileWriteTrigger(config={"pattern": "*.txt"})
        assert trigger.get_pattern() == "*.txt"


class TestFileDeleteTrigger:
    """Test FileDeleteTrigger."""

    def test_matches_pattern(self):
        """Test file delete trigger matches pattern."""
        trigger = FileDeleteTrigger(config={"pattern": "*.log"})
        assert trigger.matches({"file_path": "/logs/app.log"})
        assert not trigger.matches({"file_path": "/logs/app.txt"})

    def test_default_pattern(self):
        """Test file delete trigger with default pattern."""
        trigger = FileDeleteTrigger(config={})
        assert trigger.matches({"file_path": "/any/file.txt"})


class TestFileRenameTrigger:
    """Test FileRenameTrigger."""

    def test_matches_old_path(self):
        """Test file rename trigger matches old path."""
        trigger = FileRenameTrigger(config={"pattern": "*.tmp"})
        assert trigger.matches({"old_path": "/temp/file.tmp", "new_path": "/temp/file.txt"})

    def test_matches_new_path(self):
        """Test file rename trigger matches new path."""
        trigger = FileRenameTrigger(config={"pattern": "*.txt"})
        assert trigger.matches({"old_path": "/temp/file.tmp", "new_path": "/temp/file.txt"})

    def test_no_match(self):
        """Test file rename trigger doesn't match."""
        trigger = FileRenameTrigger(config={"pattern": "*.log"})
        assert not trigger.matches({"old_path": "/temp/file.tmp", "new_path": "/temp/file.txt"})


class TestMetadataChangeTrigger:
    """Test MetadataChangeTrigger."""

    def test_matches_file_pattern(self):
        """Test metadata change trigger matches file pattern."""
        trigger = MetadataChangeTrigger(config={"pattern": "*.md"})
        assert trigger.matches({"file_path": "/docs/readme.md", "metadata_key": "tags"})
        assert not trigger.matches({"file_path": "/docs/readme.txt", "metadata_key": "tags"})

    def test_matches_specific_key(self):
        """Test metadata change trigger matches specific key."""
        trigger = MetadataChangeTrigger(config={"pattern": "*", "metadata_key": "status"})
        assert trigger.matches({"file_path": "/test.md", "metadata_key": "status"})
        assert not trigger.matches({"file_path": "/test.md", "metadata_key": "tags"})

    def test_matches_any_key(self):
        """Test metadata change trigger matches any key."""
        trigger = MetadataChangeTrigger(config={"pattern": "*"})
        assert trigger.matches({"file_path": "/test.md", "metadata_key": "status"})
        assert trigger.matches({"file_path": "/test.md", "metadata_key": "tags"})


class TestScheduleTrigger:
    """Test ScheduleTrigger."""

    def test_create_with_cron(self):
        """Test creating schedule trigger with cron."""
        trigger = ScheduleTrigger(config={"cron": "0 0 * * *"})
        assert trigger.cron == "0 0 * * *"

    def test_create_with_interval(self):
        """Test creating schedule trigger with interval."""
        trigger = ScheduleTrigger(config={"interval_seconds": 3600})
        assert trigger.interval_seconds == 3600

    def test_does_not_match_events(self):
        """Test schedule trigger doesn't match events."""
        trigger = ScheduleTrigger(config={"cron": "0 * * * *"})
        assert not trigger.matches({"any": "event"})


class TestWebhookTrigger:
    """Test WebhookTrigger."""

    def test_matches_webhook_id(self):
        """Test webhook trigger matches ID."""
        trigger = WebhookTrigger(config={"webhook_id": "test-webhook-123"})
        assert trigger.matches({"webhook_id": "test-webhook-123"})
        assert not trigger.matches({"webhook_id": "other-webhook"})

    def test_no_webhook_id(self):
        """Test webhook trigger without ID."""
        trigger = WebhookTrigger(config={})
        assert not trigger.matches({"webhook_id": "any"})


class TestManualTrigger:
    """Test ManualTrigger."""

    def test_always_matches(self):
        """Test manual trigger always matches."""
        trigger = ManualTrigger(config={})
        assert trigger.matches({})
        assert trigger.matches({"any": "context"})


class TestBuiltinTriggers:
    """Test built-in trigger registry."""

    def test_all_triggers_registered(self):
        """Test all trigger types are in registry."""
        assert TriggerType.FILE_WRITE in BUILTIN_TRIGGERS
        assert TriggerType.FILE_DELETE in BUILTIN_TRIGGERS
        assert TriggerType.FILE_RENAME in BUILTIN_TRIGGERS
        assert TriggerType.METADATA_CHANGE in BUILTIN_TRIGGERS
        assert TriggerType.SCHEDULE in BUILTIN_TRIGGERS
        assert TriggerType.WEBHOOK in BUILTIN_TRIGGERS
        assert TriggerType.MANUAL in BUILTIN_TRIGGERS


class TestTriggerManager:
    """Test TriggerManager."""

    def test_create_manager(self):
        """Test creating trigger manager."""
        manager = TriggerManager()
        assert manager is not None
        # Should initialize with all trigger types
        for trigger_type in TriggerType:
            assert trigger_type.value in manager.triggers

    def test_register_trigger(self):
        """Test registering a trigger."""
        manager = TriggerManager()
        trigger = FileWriteTrigger(config={"pattern": "*.md"})
        callback_called = False

        def callback(context):
            nonlocal callback_called
            callback_called = True

        manager.register_trigger(trigger, callback)
        triggers = manager.get_triggers(TriggerType.FILE_WRITE)
        assert len(triggers) == 1
        assert triggers[0][0] == trigger

    def test_unregister_trigger(self):
        """Test unregistering a trigger."""
        manager = TriggerManager()
        trigger = FileWriteTrigger(config={"pattern": "*.md"})

        def callback(context):
            pass

        manager.register_trigger(trigger, callback)
        assert len(manager.get_triggers(TriggerType.FILE_WRITE)) == 1

        manager.unregister_trigger(trigger)
        assert len(manager.get_triggers(TriggerType.FILE_WRITE)) == 0

    @pytest.mark.asyncio
    async def test_fire_event_matching(self):
        """Test firing event that matches trigger."""
        manager = TriggerManager()
        trigger = FileWriteTrigger(config={"pattern": "*.md"})
        callback_called = False

        async def callback(context):
            nonlocal callback_called
            callback_called = True

        manager.register_trigger(trigger, callback)

        count = await manager.fire_event(TriggerType.FILE_WRITE, {"file_path": "/docs/readme.md"})

        assert count == 1
        assert callback_called

    @pytest.mark.asyncio
    async def test_fire_event_no_match(self):
        """Test firing event that doesn't match trigger."""
        manager = TriggerManager()
        trigger = FileWriteTrigger(config={"pattern": "*.md"})
        callback_called = False

        async def callback(context):
            nonlocal callback_called
            callback_called = True

        manager.register_trigger(trigger, callback)

        count = await manager.fire_event(TriggerType.FILE_WRITE, {"file_path": "/docs/readme.txt"})

        assert count == 0
        assert not callback_called

    @pytest.mark.asyncio
    async def test_fire_event_multiple_triggers(self):
        """Test firing event that matches multiple triggers."""
        manager = TriggerManager()
        trigger1 = FileWriteTrigger(config={"pattern": "*.md"})
        trigger2 = FileWriteTrigger(config={"pattern": "*.md"})
        callback1_called = False
        callback2_called = False

        async def callback1(context):
            nonlocal callback1_called
            callback1_called = True

        async def callback2(context):
            nonlocal callback2_called
            callback2_called = True

        manager.register_trigger(trigger1, callback1)
        manager.register_trigger(trigger2, callback2)

        count = await manager.fire_event(TriggerType.FILE_WRITE, {"file_path": "/docs/readme.md"})

        assert count == 2
        assert callback1_called
        assert callback2_called

    @pytest.mark.asyncio
    async def test_fire_event_with_error(self):
        """Test firing event when callback raises error."""
        manager = TriggerManager()
        trigger = FileWriteTrigger(config={"pattern": "*.md"})

        async def failing_callback(context):
            raise ValueError("Test error")

        manager.register_trigger(trigger, failing_callback)

        # Should not raise, just log error
        count = await manager.fire_event(TriggerType.FILE_WRITE, {"file_path": "/docs/readme.md"})

        # Count should be 0 because callback failed
        assert count == 0

    def test_get_triggers_all(self):
        """Test getting all triggers."""
        manager = TriggerManager()
        trigger1 = FileWriteTrigger(config={"pattern": "*.md"})
        trigger2 = FileDeleteTrigger(config={"pattern": "*.log"})

        def callback1(context):
            pass

        def callback2(context):
            pass

        manager.register_trigger(trigger1, callback1)
        manager.register_trigger(trigger2, callback2)

        all_triggers = manager.get_triggers()
        assert len(all_triggers) == 2

    def test_get_triggers_by_type(self):
        """Test getting triggers by type."""
        manager = TriggerManager()
        trigger1 = FileWriteTrigger(config={"pattern": "*.md"})
        trigger2 = FileDeleteTrigger(config={"pattern": "*.log"})

        def callback1(context):
            pass

        def callback2(context):
            pass

        manager.register_trigger(trigger1, callback1)
        manager.register_trigger(trigger2, callback2)

        write_triggers = manager.get_triggers(TriggerType.FILE_WRITE)
        assert len(write_triggers) == 1

        delete_triggers = manager.get_triggers(TriggerType.FILE_DELETE)
        assert len(delete_triggers) == 1
