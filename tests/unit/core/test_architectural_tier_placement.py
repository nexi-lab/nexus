"""Architectural tier placement regression tests (Issues #2360, #2366).

These tests verify that key modules remain in their correct architectural tier
per NEXUS-LEGO-ARCHITECTURE.md. They act as living documentation — if a future
refactor accidentally moves a file, these tests will catch it.
"""

import importlib


class TestPipeTierPlacement:
    """pipe.py (MemoryPipeBackend/kfifo) is a kernel VFS primitive — stays in core/.
    pipe_manager.py (PipeManager/fs/pipe.c) is a kernel primitive — lives in core/.

    Issue #2366: PipeManager reverted back to core/ — it is a kernel primitive
    alongside MemoryPipeBackend per KERNEL-ARCHITECTURE.md.
    """

    def test_ring_buffer_in_core(self) -> None:
        mod = importlib.import_module("nexus.core.pipe")
        assert hasattr(mod, "MemoryPipeBackend")

    def test_pipe_manager_in_core(self) -> None:
        mod = importlib.import_module("nexus.core.pipe_manager")
        assert hasattr(mod, "PipeManager")


class TestSchedulerTierPlacement:
    """Scheduler is a system service — MUST be in system_services/ and wired via factory (Issue #2360)."""

    def test_scheduler_protocol_in_contracts(self) -> None:
        mod = importlib.import_module("nexus.contracts.protocols.scheduler")
        assert hasattr(mod, "SchedulerProtocol")
        assert hasattr(mod, "CreditsReservationProtocol")
        assert hasattr(mod, "NullCreditsReservation")

    def test_in_memory_scheduler_in_system_services(self) -> None:
        mod = importlib.import_module("nexus.services.scheduler.in_memory")
        assert hasattr(mod, "InMemoryScheduler")

    def test_classify_agent_request_in_system_services(self) -> None:
        mod = importlib.import_module("nexus.services.scheduler.policies.classifier")
        assert hasattr(mod, "classify_agent_request")

    def test_scheduler_service_in_system_services(self) -> None:
        mod = importlib.import_module("nexus.services.scheduler.service")
        assert hasattr(mod, "SchedulerService")
