"""Nexus Scheduler - Hybrid priority task scheduling system.

Provides a 4-layer priority system for agent task scheduling:
1. Fixed priority tiers (CRITICAL > HIGH > NORMAL > LOW > BEST_EFFORT)
2. Within-tier ordering (deadline proximity, FIFO, boost)
3. Anti-starvation aging (tasks gain priority over time)
4. Capped price boost (pay for max +2 tier boost)

Astraea extensions (Issue #1274):
5. Request classification (INTERACTIVE / BATCH / BACKGROUND)
6. HRRN scoring within priority classes
7. Per-agent fair-share admission control
8. Agent state event awareness

Related: Issue #1212, #1274
"""

from nexus.system_services.scheduler.constants import PriorityClass, PriorityTier, RequestState
from nexus.system_services.scheduler.dispatcher import TaskDispatcher
from nexus.system_services.scheduler.events import AgentStateEmitter, AgentStateEvent
from nexus.system_services.scheduler.models import ScheduledTask, TaskSubmission
from nexus.system_services.scheduler.service import SchedulerService

__all__ = [
    "AgentStateEmitter",
    "AgentStateEvent",
    "PriorityClass",
    "PriorityTier",
    "RequestState",
    "ScheduledTask",
    "SchedulerService",
    "TaskDispatcher",
    "TaskSubmission",
]
