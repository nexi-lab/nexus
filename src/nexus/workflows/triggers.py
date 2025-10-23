"""Workflow trigger system."""

import fnmatch
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from nexus.workflows.types import TriggerType, WorkflowContext

logger = logging.getLogger(__name__)


class BaseTrigger(ABC):
    """Base class for workflow triggers."""

    def __init__(self, trigger_type: TriggerType, config: dict[str, Any]):
        self.trigger_type = trigger_type
        self.config = config

    @abstractmethod
    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if this trigger matches the given event.

        Args:
            event_context: Event context with file path, metadata, etc.

        Returns:
            True if trigger matches
        """
        pass

    def get_pattern(self) -> str | None:
        """Get the file pattern for this trigger."""
        return self.config.get("pattern")


class FileWriteTrigger(BaseTrigger):
    """Trigger on file write events."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.FILE_WRITE, config)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if file path matches the pattern."""
        file_path = event_context.get("file_path", "")
        return fnmatch.fnmatch(file_path, self.pattern)


class FileDeleteTrigger(BaseTrigger):
    """Trigger on file delete events."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.FILE_DELETE, config)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if file path matches the pattern."""
        file_path = event_context.get("file_path", "")
        return fnmatch.fnmatch(file_path, self.pattern)


class FileRenameTrigger(BaseTrigger):
    """Trigger on file rename events."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.FILE_RENAME, config)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if old or new file path matches the pattern."""
        old_path = event_context.get("old_path", "")
        new_path = event_context.get("new_path", "")
        return fnmatch.fnmatch(old_path, self.pattern) or fnmatch.fnmatch(new_path, self.pattern)


class MetadataChangeTrigger(BaseTrigger):
    """Trigger on metadata change events."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.METADATA_CHANGE, config)
        self.pattern = config.get("pattern", "*")
        self.metadata_key = config.get("metadata_key")

    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if metadata change matches criteria."""
        file_path = event_context.get("file_path", "")
        changed_key = event_context.get("metadata_key")

        # Check file path pattern
        if not fnmatch.fnmatch(file_path, self.pattern):
            return False

        # Check specific metadata key if specified
        return not (self.metadata_key and changed_key != self.metadata_key)


class ScheduleTrigger(BaseTrigger):
    """Trigger on a schedule (cron-like)."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.SCHEDULE, config)
        self.cron = config.get("cron", "0 * * * *")  # Default: hourly
        self.interval_seconds = config.get("interval_seconds")

    def matches(self, _event_context: dict[str, Any]) -> bool:
        """Schedule triggers don't match events directly."""
        return False


class WebhookTrigger(BaseTrigger):
    """Trigger via HTTP webhook."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.WEBHOOK, config)
        self.webhook_id = config.get("webhook_id")

    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if webhook ID matches."""
        return event_context.get("webhook_id") == self.webhook_id


class ManualTrigger(BaseTrigger):
    """Manual trigger (via CLI/API)."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(TriggerType.MANUAL, config)

    def matches(self, _event_context: dict[str, Any]) -> bool:
        """Manual triggers always match when explicitly invoked."""
        return True


# Built-in trigger registry
BUILTIN_TRIGGERS = {
    TriggerType.FILE_WRITE: FileWriteTrigger,
    TriggerType.FILE_DELETE: FileDeleteTrigger,
    TriggerType.FILE_RENAME: FileRenameTrigger,
    TriggerType.METADATA_CHANGE: MetadataChangeTrigger,
    TriggerType.SCHEDULE: ScheduleTrigger,
    TriggerType.WEBHOOK: WebhookTrigger,
    TriggerType.MANUAL: ManualTrigger,
}


class TriggerManager:
    """Manages workflow triggers and event routing."""

    def __init__(self) -> None:
        self.triggers: dict[str, list[tuple[BaseTrigger, Callable]]] = {}
        for trigger_type in TriggerType:
            self.triggers[trigger_type.value] = []

    def register_trigger(
        self, trigger: BaseTrigger, callback: Callable[[WorkflowContext], None]
    ) -> None:
        """Register a trigger with a callback.

        Args:
            trigger: Trigger instance
            callback: Function to call when trigger fires
        """
        trigger_type = trigger.trigger_type.value
        self.triggers[trigger_type].append((trigger, callback))
        logger.info(f"Registered {trigger_type} trigger with pattern: {trigger.get_pattern()}")

    def unregister_trigger(self, trigger: BaseTrigger) -> None:
        """Unregister a trigger.

        Args:
            trigger: Trigger instance to remove
        """
        trigger_type = trigger.trigger_type.value
        self.triggers[trigger_type] = [
            (t, cb) for t, cb in self.triggers[trigger_type] if t != trigger
        ]

    async def fire_event(self, trigger_type: TriggerType, event_context: dict[str, Any]) -> int:
        """Fire an event and execute matching triggers.

        Args:
            trigger_type: Type of trigger event
            event_context: Event context data

        Returns:
            Number of workflows triggered
        """
        triggered_count = 0
        trigger_list = self.triggers.get(trigger_type.value, [])

        for trigger, callback in trigger_list:
            if trigger.matches(event_context):
                try:
                    await callback(event_context)
                    triggered_count += 1
                except Exception as e:
                    logger.error(f"Error executing trigger callback: {e}")

        return triggered_count

    def get_triggers(self, trigger_type: TriggerType | None = None) -> list[tuple]:
        """Get registered triggers.

        Args:
            trigger_type: Optional filter by trigger type

        Returns:
            List of (trigger, callback) tuples
        """
        if trigger_type:
            return self.triggers.get(trigger_type.value, [])

        # Return all triggers
        all_triggers = []
        for trigger_list in self.triggers.values():
            all_triggers.extend(trigger_list)
        return all_triggers
