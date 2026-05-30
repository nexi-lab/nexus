"""Shared control-plane surface coverage model for operator workflows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ControlPlaneSurface:
    """Documented and tested external surface for the full-profile control plane."""

    profile: str
    module_group: str
    surface: str
    rpc_methods: tuple[str, ...]
    cli_commands: tuple[str, ...]
    transports: tuple[str, ...]
    how_to_use: str
    admin_only: bool
    profile_gate: str
    correctness_tests: tuple[str, ...]
    performance_classification: str
    gap_issue: str | None = None


CONTROL_PLANE_SURFACES: tuple[ControlPlaneSurface, ...] = (
    ControlPlaneSurface(
        profile="full",
        module_group="admin",
        surface="Admin API key and permission management",
        rpc_methods=(
            "admin_write_permission",
            "admin_create_key",
            "admin_list_keys",
            "admin_get_key",
            "admin_revoke_key",
            "admin_update_key",
        ),
        cli_commands=(
            "nexus admin create-user",
            "nexus admin create-key",
            "nexus admin create-agent-key",
            "nexus admin list-users",
            "nexus admin get-user",
            "nexus admin revoke-key",
            "nexus admin update-key",
        ),
        transports=("CLI", "generic gRPC Call", "HTTP auth key API"),
        how_to_use="Use after database auth is enabled to mint, inspect, rotate, and revoke user or agent API keys.",
        admin_only=True,
        profile_gate="full/cloud server with DatabaseAPIKeyAuth and an admin token",
        correctness_tests=(
            "tests/unit/server/test_admin_handlers.py",
            "tests/unit/server/test_rpc_admin_only.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; key RPCs benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="audit",
        surface="Audit list and export",
        rpc_methods=("audit_list", "audit_export"),
        cli_commands=("nexus audit list", "nexus audit export"),
        transports=("CLI", "generic gRPC Call", "HTTP audit APIs where present"),
        how_to_use="Use to inspect or export exchange/payment audit records for compliance and incident review.",
        admin_only=True,
        profile_gate="full/cloud server with record store and exchange audit logger",
        correctness_tests=(
            "tests/conformance/test_exchange_openapi.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="events",
        surface="Event replay",
        rpc_methods=("events_replay",),
        cli_commands=("nexus events replay",),
        transports=("CLI", "generic gRPC Call"),
        how_to_use="Use to replay historical file/activity events for operational investigation.",
        admin_only=True,
        profile_gate="full/cloud server with record store and event replay service",
        correctness_tests=(
            "tests/e2e/server/test_event_stream_e2e.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="governance",
        surface="Governance status, alerts, and rings",
        rpc_methods=("governance_status", "governance_alerts", "governance_rings"),
        cli_commands=(
            "nexus governance status",
            "nexus governance alerts",
            "nexus governance rings",
        ),
        transports=("CLI", "generic gRPC Call"),
        how_to_use="Use in marketplace/operator deployments to review anomaly alerts and collusion findings.",
        admin_only=True,
        profile_gate="full/cloud server with governance services wired",
        correctness_tests=(
            "tests/unit/server/test_security_hardening.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="federation",
        surface="Federation read-only introspection",
        rpc_methods=(
            "federation_client_whoami",
            "federation_list_zones",
            "federation_cluster_info",
        ),
        cli_commands=(
            "nexus federation status",
            "nexus federation zones",
            "nexus federation info",
        ),
        transports=("CLI", "generic gRPC Call"),
        how_to_use="Use to inspect zone grants, zone inventory, and cluster status before making federation changes.",
        admin_only=False,
        profile_gate="federation runtime active; cluster/cloud deployments, or full server with federation kernel support",
        correctness_tests=(
            "tests/unit/grpc/test_federation_whoami_rpc.py",
            "tests/e2e/docker/test_federation_e2e.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; federation_list_zones benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="federation",
        surface="Federation zone lifecycle and mounts",
        rpc_methods=(
            "federation_export_zone",
            "federation_import_zone",
            "federation_create_zone",
            "federation_remove_zone",
            "federation_join",
            "federation_mount",
            "federation_unmount",
            "federation_share",
        ),
        cli_commands=(
            "nexus federation mount",
            "nexus federation unmount",
            "generic gRPC Call for create/remove/share/join until #4200 lands",
        ),
        transports=("CLI", "generic gRPC Call"),
        how_to_use="Use admin credentials to create, remove, share, join, mount, unmount, export, or import federation zones.",
        admin_only=True,
        profile_gate="federation runtime active; cluster/cloud deployments, or full server with federation kernel support",
        correctness_tests=(
            "tests/e2e/docker/test_federation_e2e.py",
            "tests/e2e/server/test_zone_export_e2e.py",
            "tests/e2e/server/test_zone_import_e2e.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
            "tests/benchmarks/test_full_control_plane_rpc_benchmark.py",
        ),
        performance_classification="control plane hot path; federation_create_zone benchmarked in tests/benchmarks/test_full_control_plane_rpc_benchmark.py; CLI parity gap #4200",
        gap_issue="#4200",
    ),
    ControlPlaneSurface(
        profile="full",
        module_group="pay",
        surface="Pay balance, transfer, and history",
        rpc_methods=("pay_balance", "pay_transfer", "pay_history"),
        cli_commands=("nexus pay balance", "nexus pay transfer", "nexus pay history"),
        transports=("CLI", "generic gRPC Call", "HTTP pay APIs"),
        how_to_use="Use as an authenticated marketplace user or agent to inspect credits, transfer funds, and review payment history.",
        admin_only=False,
        profile_gate="full/cloud server with pay brick enabled",
        correctness_tests=(
            "tests/conformance/test_exchange_openapi.py",
            "tests/e2e/self_contained/pay/test_x402_integration.py",
            "tests/unit/server/test_full_profile_control_plane_surface.py",
        ),
        performance_classification="marketplace user path; not classified as an admin/operator hot path in #4138",
    ),
)
