"""Architectural tier placement regression tests (Issues #2360, #2366).

These tests verify that key modules remain in their correct architectural tier
per NEXUS-LEGO-ARCHITECTURE.md. They act as living documentation — if a future
refactor accidentally moves a file, these tests will catch it.
"""

import importlib


class TestPipeTierPlacement:
    """pipe.py (PipeBackend protocol + exceptions) is a kernel VFS primitive — stays in core/.
    PipeManager (fs/pipe.c equivalent) is now a Rust-native kernel primitive exposed via
    nexus_kernel.Kernel — no Python module for it.

    Issue #2366: PipeManager is a kernel primitive per KERNEL-ARCHITECTURE.md.
    Post IPC-Rust-ification (2026-04-07): PipeManager lives in the Rust kernel binary,
    accessed through nexus_kernel.Kernel.{create_pipe, destroy_pipe, pipe_read_nowait,
    pipe_write_nowait, ...}.
    """

    def test_pipe_backend_protocol_in_core(self) -> None:
        mod = importlib.import_module("nexus.core.pipe")
        assert hasattr(mod, "PipeBackend")

    def test_pipe_manager_in_rust_kernel(self) -> None:
        """PipeManager is now a Rust kernel primitive — assert it's exposed on nexus_kernel.Kernel."""
        nexus_kernel = importlib.import_module("nexus_kernel")
        kernel_cls = nexus_kernel.Kernel
        for method in (
            "create_pipe",
            "destroy_pipe",
            "close_pipe",
            "close_all_pipes",
            "has_pipe",
            "list_pipes",
            "pipe_read_nowait",
            "pipe_write_nowait",
        ):
            assert hasattr(kernel_cls, method), (
                f"nexus_kernel.Kernel missing pipe primitive: {method}"
            )


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
