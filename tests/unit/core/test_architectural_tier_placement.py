"""Architectural tier placement regression tests (Issues #2360, #2366).

These tests verify that key modules remain in their correct architectural tier
per NEXUS-LEGO-ARCHITECTURE.md. They act as living documentation — if a future
refactor accidentally moves a file, these tests will catch it.
"""

from __future__ import annotations

import importlib


class TestPipeManagerTierPlacement:
    """pipe.py is a kernel VFS primitive — MUST stay in core/ (Issue #2366).

    Rationale: KERNEL-ARCHITECTURE.md §6.2 classifies PipeManager as kernel-tier
    IPC (equivalent to Linux fs/pipe.c). It manages DT_PIPE inodes via MetastoreABC.
    """

    def test_pipe_module_in_core(self) -> None:
        mod = importlib.import_module("nexus.core.pipe")
        assert hasattr(mod, "RingBuffer")


class TestSchedulerTierPlacement:
    """Scheduler is a system service — MUST be in services/ and wired via factory (Issue #2360)."""

    def test_scheduler_protocol_in_services(self) -> None:
        mod = importlib.import_module("nexus.services.protocols.scheduler")
        assert hasattr(mod, "SchedulerProtocol")
        assert hasattr(mod, "InMemoryScheduler")
        assert hasattr(mod, "CreditsReservationProtocol")
        assert hasattr(mod, "NullCreditsReservation")
        assert hasattr(mod, "classify_agent_request")

    def test_scheduler_service_in_services(self) -> None:
        mod = importlib.import_module("nexus.services.scheduler.service")
        assert hasattr(mod, "SchedulerService")
