"""OpenTelemetry instrumentation for Nexus.

This module provides observability through OpenTelemetry, enabling distributed
tracing, metrics, and logging that can be exported to any OTLP-compatible backend
(SigNoz, Grafana Tempo, Jaeger, etc.).

Environment Variables:
    OTEL_ENABLED: Enable/disable telemetry (default: "false")
    OTEL_SERVICE_NAME: Service name for traces (default: "nexus")
    OTEL_EXPORTER_OTLP_ENDPOINT: OTLP endpoint URL (default: "http://localhost:4317")
    OTEL_EXPORTER_OTLP_INSECURE: Use insecure connection (default: "true")
    OTEL_TRACES_SAMPLER: Sampling strategy (default: "parentbased_traceidratio")
    OTEL_TRACES_SAMPLER_ARG: Sampling ratio 0.0-1.0 (default: "1.0")

Usage:
    from nexus.server.telemetry import setup_telemetry, get_tracer

    # Initialize once at startup
    setup_telemetry()

    # Get a tracer for custom spans
    tracer = get_tracer(__name__)

    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("custom.attribute", "value")
        # ... your code ...

Example with SigNoz:
    OTEL_ENABLED=true
    OTEL_SERVICE_NAME=nexus
    OTEL_EXPORTER_OTLP_ENDPOINT=http://signoz:4317
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

# Global state
_initialized = False
_tracer: Tracer | None = None


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled via environment variable."""
    return os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")


def setup_telemetry(
    service_name: str | None = None,
    endpoint: str | None = None,
    insecure: bool | None = None,
    sample_ratio: float | None = None,
) -> bool:
    """Initialize OpenTelemetry instrumentation.

    This sets up:
    - Trace provider with OTLP exporter
    - Auto-instrumentation for FastAPI, HTTPX, SQLAlchemy, Redis, aiohttp

    Args:
        service_name: Override OTEL_SERVICE_NAME env var
        endpoint: Override OTEL_EXPORTER_OTLP_ENDPOINT env var
        insecure: Override OTEL_EXPORTER_OTLP_INSECURE env var
        sample_ratio: Override OTEL_TRACES_SAMPLER_ARG env var (0.0-1.0)

    Returns:
        True if telemetry was initialized, False if disabled or already initialized
    """
    global _initialized, _tracer

    if _initialized:
        logger.debug("Telemetry already initialized, skipping")
        return False

    if not is_telemetry_enabled():
        logger.info("OpenTelemetry disabled (set OTEL_ENABLED=true to enable)")
        return False

    try:
        # Import OpenTelemetry components
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

        # Configuration from args or environment
        _service_name = service_name or os.environ.get("OTEL_SERVICE_NAME", "nexus")
        _endpoint = endpoint or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
        )
        _insecure = (
            insecure
            if insecure is not None
            else (
                os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "true").lower()
                in ("true", "1", "yes")
            )
        )
        _sample_ratio = (
            sample_ratio
            if sample_ratio is not None
            else float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1.0"))
        )

        # Create resource with service info
        resource = Resource.create(
            {
                "service.name": _service_name,
                "service.version": _get_version(),
                "deployment.environment": os.environ.get("OTEL_ENVIRONMENT", "development"),
            }
        )

        # Create sampler (parentbased respects parent span's sampling decision)
        sampler = ParentBasedTraceIdRatio(_sample_ratio)

        # Create tracer provider
        provider = TracerProvider(resource=resource, sampler=sampler)

        # Create OTLP exporter
        exporter = OTLPSpanExporter(
            endpoint=_endpoint,
            insecure=_insecure,
        )

        # Use batch processor for efficiency
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)

        # Set as global tracer provider
        trace.set_tracer_provider(provider)

        # Store tracer for get_tracer()
        _tracer = trace.get_tracer(__name__)

        # Auto-instrument libraries
        _instrument_libraries()

        _initialized = True
        logger.info(
            f"OpenTelemetry initialized: service={_service_name}, "
            f"endpoint={_endpoint}, sample_ratio={_sample_ratio}"
        )
        return True

    except ImportError as e:
        logger.warning(f"OpenTelemetry packages not installed: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}")
        return False


def _get_version() -> str:
    """Get Nexus version for resource attributes."""
    try:
        from importlib.metadata import version

        return version("nexus-ai-fs")
    except Exception:
        return "unknown"


def _instrument_libraries() -> None:
    """Auto-instrument supported libraries."""
    # FastAPI (will be instrumented when app is created)
    # We don't instrument here - see instrument_fastapi_app()

    # HTTPX - async HTTP client
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.debug("Instrumented: httpx")
    except ImportError:
        logger.debug("httpx instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument httpx: {e}")

    # SQLAlchemy - database
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(
            enable_commenter=True,
            commenter_options={},
        )
        logger.debug("Instrumented: sqlalchemy (with commenter)")
    except ImportError:
        logger.debug("sqlalchemy instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument sqlalchemy: {e}")

    # Redis - cache
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
        logger.debug("Instrumented: redis")
    except ImportError:
        logger.debug("redis instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument redis: {e}")

    # aiohttp - async HTTP client (used by some backends)
    try:
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor

        AioHttpClientInstrumentor().instrument()
        logger.debug("Instrumented: aiohttp")
    except ImportError:
        logger.debug("aiohttp instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument aiohttp: {e}")


def instrument_fastapi_app(app: object) -> bool:
    """Instrument a FastAPI application.

    This should be called after creating the FastAPI app but before
    adding routes. It adds middleware for automatic request tracing.

    Args:
        app: FastAPI application instance

    Returns:
        True if instrumented, False if telemetry disabled or error
    """
    if not is_telemetry_enabled() or not _initialized:
        return False

    try:
        from typing import cast

        from fastapi import FastAPI
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(cast(FastAPI, app))
        logger.debug("Instrumented: FastAPI app")
        return True
    except ImportError:
        logger.debug("FastAPI instrumentation not available")
        return False
    except Exception as e:
        logger.warning(f"Failed to instrument FastAPI: {e}")
        return False


def get_tracer(name: str | None = None) -> Tracer | None:
    """Get a tracer for creating custom spans.

    Args:
        name: Tracer name (typically __name__ of the module)

    Returns:
        Tracer instance or None if telemetry is disabled

    Example:
        tracer = get_tracer(__name__)
        if tracer:
            with tracer.start_as_current_span("my_operation") as span:
                span.set_attribute("key", "value")
                # ... your code ...
    """
    if not _initialized:
        return None

    try:
        from opentelemetry import trace

        return trace.get_tracer(name or __name__)
    except Exception:
        return None


def add_span_attribute(key: str, value: str | int | float | bool) -> None:
    """Add an attribute to the current span.

    This is a convenience function for adding attributes without
    needing to manage span context directly.

    Args:
        key: Attribute key
        value: Attribute value
    """
    if not _initialized:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span:
            span.set_attribute(key, value)
    except Exception:
        pass


def record_exception(exception: Exception) -> None:
    """Record an exception in the current span.

    Args:
        exception: The exception to record
    """
    if not _initialized:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span:
            span.record_exception(exception)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exception)))
    except Exception:
        pass


def shutdown_telemetry() -> None:
    """Shutdown telemetry and flush pending spans.

    Call this during application shutdown to ensure all spans are exported.
    """
    global _initialized, _tracer

    if not _initialized:
        return

    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
        logger.info("OpenTelemetry shutdown complete")
    except Exception as e:
        logger.warning(f"Error during OpenTelemetry shutdown: {e}")
    finally:
        _initialized = False
        _tracer = None
