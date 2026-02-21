"""Centralized default constants for Nexus.

Avoids hardcoded URLs, ports, and magic values scattered across the codebase.
All defaults can be overridden via environment variables or configuration.

Related: Issue #1462 (code hygiene — stale TODOs, redundant imports, hardcoded URLs)
"""

# =============================================================================
# Server Defaults
# =============================================================================

DEFAULT_NEXUS_URL = "http://localhost:2026"
"""Default Nexus API server URL. Override via NEXUS_URL env var."""

DEFAULT_NEXUS_PORT = 2026
"""Default Nexus API server port."""

DEFAULT_GRPC_BIND_ADDR = "0.0.0.0:2126"
"""Default Raft gRPC bind address. Override via NEXUS_BIND_ADDR env var."""

DEFAULT_LANGGRAPH_URL = "http://localhost:2024"
"""Default LangGraph server URL. Override via LANGGRAPH_SERVER_URL env var."""

# =============================================================================
# OAuth Defaults
# =============================================================================

DEFAULT_OAUTH_REDIRECT_URI = "http://localhost:3000/oauth/callback"
"""Default OAuth redirect URI for local development."""

DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:5173/oauth/callback"
"""Default Google OAuth redirect URI (frontend dev server)."""

# =============================================================================
# Observability Defaults
# =============================================================================

DEFAULT_OTEL_ENDPOINT = "http://localhost:4317"
"""Default OpenTelemetry OTLP endpoint. Override via OTEL_EXPORTER_OTLP_ENDPOINT env var."""

# =============================================================================
# Search Defaults
# =============================================================================

DEFAULT_ZOEKT_URL = "http://localhost:6070"
"""Default Zoekt code search server URL. Override via ZOEKT_URL env var."""

# =============================================================================
# Event Bus Defaults
# =============================================================================

DEFAULT_NATS_URL = "nats://localhost:4222"
"""Default NATS JetStream server URL. Override via NEXUS_NATS_URL env var."""

# =============================================================================
# Zone Defaults
# =============================================================================

ROOT_ZONE_ID = "root"
"""Default zone ID for standalone (non-federated) deployments.

Every NexusFS instance has a zone_id. In standalone mode it defaults to
``"root"``. In federated mode each zone has a unique ID assigned by
the Raft consensus layer. This is a kernel concept — raft and other
layers import from here.
"""
