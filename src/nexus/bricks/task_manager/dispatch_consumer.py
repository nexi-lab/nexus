"""Compatibility shim for the canonical task dispatch consumer."""

from nexus.task_manager.dispatch_consumer import LLMCallable, TaskDispatchPipeConsumer

__all__ = ["LLMCallable", "TaskDispatchPipeConsumer"]
