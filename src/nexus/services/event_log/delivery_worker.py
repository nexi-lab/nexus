"""Backward-compatible re-export — canonical module is event_subsystem.log.delivery.

Issue #3193: consolidated to single implementation.
"""

from nexus.services.event_subsystem.log.delivery import EventDeliveryWorker

__all__ = ["EventDeliveryWorker"]
