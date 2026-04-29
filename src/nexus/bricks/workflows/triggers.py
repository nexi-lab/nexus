"""Workflow trigger system.

Zero imports from nexus.core — glob matching is injected via GlobMatchFn.
Falls back to fnmatch when no glob_match function is injected (tests, embedded).
"""

import fnmatch
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from nexus.bricks.workflows.protocol import GlobMatchFn
from nexus.bricks.workflows.types import TriggerType, WorkflowContext


@runtime_checkable
class TriggerFactory(Protocol):
    """Callable that creates a concrete BaseTrigger (used for registry typing)."""

    def __call__(
        self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None
    ) -> "BaseTrigger": ...


logger = logging.getLogger(__name__)

_fnmatch_warned = False


def _default_glob_match(path: str, patterns: list[str]) -> bool:
    """Fallback glob match using stdlib fnmatch (no Rust dependency)."""
    global _fnmatch_warned
    if not _fnmatch_warned:
        logger.warning(
            "Using fnmatch fallback for workflow triggers — "
            "inject glob_match for production performance"
        )
        _fnmatch_warned = True
    return any(fnmatch.fnmatch(path, p) for p in patterns)


class BaseTrigger(ABC):
    """Base class for workflow triggers."""

    def __init__(
        self,
        trigger_type: TriggerType,
        config: dict[str, Any],
        *,
        glob_match: GlobMatchFn | None = None,
    ):
        self.trigger_type = trigger_type
        self.config = config
        self._glob_match: GlobMatchFn = glob_match or _default_glob_match

    @abstractmethod
    def matches(self, event_context: dict[str, Any]) -> bool:
        """Check if this trigger matches the given event."""
        pass

    def get_pattern(self) -> str | None:
        """Get the file pattern for this trigger."""
        return self.config.get("pattern")


class FileWriteTrigger(BaseTrigger):
    """Trigger on file write events."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.FILE_WRITE, config, glob_match=glob_match)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        file_path = event_context.get("file_path", "")
        return self._glob_match(file_path, [self.pattern])


class FileDeleteTrigger(BaseTrigger):
    """Trigger on file delete events."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.FILE_DELETE, config, glob_match=glob_match)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        file_path = event_context.get("file_path", "")
        return self._glob_match(file_path, [self.pattern])


class FileRenameTrigger(BaseTrigger):
    """Trigger on file rename events."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.FILE_RENAME, config, glob_match=glob_match)
        self.pattern = config.get("pattern", "*")

    def matches(self, event_context: dict[str, Any]) -> bool:
        old_path = event_context.get("old_path", "")
        new_path = event_context.get("new_path", "")
        return self._glob_match(old_path, [self.pattern]) or self._glob_match(
            new_path, [self.pattern]
        )


class MetadataChangeTrigger(BaseTrigger):
    """Trigger on metadata change events."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.METADATA_CHANGE, config, glob_match=glob_match)
        self.pattern = config.get("pattern", "*")
        self.metadata_key = config.get("metadata_key")

    def matches(self, event_context: dict[str, Any]) -> bool:
        file_path = event_context.get("file_path", "")
        changed_key = event_context.get("metadata_key")

        if not self._glob_match(file_path, [self.pattern]):
            return False

        return not (self.metadata_key and changed_key != self.metadata_key)


class ScheduleTrigger(BaseTrigger):
    """Trigger on a schedule (cron-like)."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.SCHEDULE, config, glob_match=glob_match)
        self.cron = config.get("cron", "0 * * * *")
        self.interval_seconds = config.get("interval_seconds")

    def matches(self, _event_context: dict[str, Any]) -> bool:
        # Schedule triggers always match when fired by the scheduler.
        # The scheduler is responsible for evaluating cron/interval timing;
        # fire_event(TriggerType.SCHEDULE, ...) means "it's time to fire."
        return True


class WebhookTrigger(BaseTrigger):
    """Trigger via HTTP webhook."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.WEBHOOK, config, glob_match=glob_match)
        self.webhook_id = config.get("webhook_id")

    def matches(self, event_context: dict[str, Any]) -> bool:
        return event_context.get("webhook_id") == self.webhook_id


class ManualTrigger(BaseTrigger):
    """Manual trigger (via CLI/API)."""

    def __init__(self, config: dict[str, Any], *, glob_match: GlobMatchFn | None = None):
        super().__init__(TriggerType.MANUAL, config, glob_match=glob_match)

    def matches(self, _event_context: dict[str, Any]) -> bool:
        return True


# Built-in trigger registry
BUILTIN_TRIGGERS: dict[TriggerType, TriggerFactory] = {
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

    def __init__(self, *, glob_match: GlobMatchFn | None = None) -> None:
        self._glob_match = glob_match
        self.triggers: dict[str, list[tuple[BaseTrigger, Callable]]] = {}
        for trigger_type in TriggerType:
            self.triggers[trigger_type.value] = []

    def register_trigger(
        self, trigger: BaseTrigger, callback: Callable[[WorkflowContext], None]
    ) -> None:
        """Register a trigger with a callback."""
        trigger_type = trigger.trigger_type.value
        self.triggers[trigger_type].append((trigger, callback))
        logger.info(f"Registered {trigger_type} trigger with pattern: {trigger.get_pattern()}")

    def unregister_trigger(self, trigger: BaseTrigger) -> None:
        """Unregister a trigger."""
        trigger_type = trigger.trigger_type.value
        self.triggers[trigger_type] = [
            (t, cb) for t, cb in self.triggers[trigger_type] if t != trigger
        ]

    async def fire_event(
        self, trigger_type: TriggerType | str, event_context: dict[str, Any]
    ) -> int:
        """Fire an event and execute matching triggers.

        Args:
            trigger_type: TriggerType enum or string value.
            event_context: Event context data.

        Returns:
            Number of workflows triggered.
        """
        key = trigger_type.value if isinstance(trigger_type, TriggerType) else trigger_type
        triggered_count = 0
        trigger_list = self.triggers.get(key, [])

        for trigger, callback in trigger_list:
            if trigger.matches(event_context):
                try:
                    await callback(event_context)
                    triggered_count += 1
                except Exception as e:
                    logger.error(f"Error executing trigger callback: {e}")

        return triggered_count

    def get_triggers(self, trigger_type: TriggerType | None = None) -> list[tuple]:
        """Get registered triggers."""
        if trigger_type:
            return self.triggers.get(trigger_type.value, [])

        all_triggers = []
        for trigger_list in self.triggers.values():
            all_triggers.extend(trigger_list)
        return all_triggers
