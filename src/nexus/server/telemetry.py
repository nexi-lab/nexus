"""OpenTelemetry instrumentation for Nexus (server layer).

Server-specific setup and instrumentation. Tier-neutral utilities
(get_tracer, is_telemetry_enabled, etc.) live in ``nexus.lib.telemetry``
so that services and backends can use them without importing from server/.

Environment Variables:
    OTEL_ENABLED: Enable/disable telemetry (default: "false")
    OTEL_SERVICE_NAME: Service name for traces (default: "nexus")
    OTEL_EXPORTER_OTLP_ENDPOINT: OTLP endpoint URL (default: "http://localhost:4317")
    OTEL_EXPORTER_OTLP_INSECURE: Use insecure connection (default: "true")
    OTEL_TRACES_SAMPLER: Sampling strategy (default: "parentbased_traceidratio")
    OTEL_TRACES_SAMPLER_ARG: Sampling ratio 0.0-1.0 (default: "1.0")

Usage:
    from nexus.lib.telemetry import get_tracer  # tier-neutral
    from nexus.server.telemetry import setup_telemetry  # server-only

    setup_telemetry()
    tracer = get_tracer(__name__)
"""

import logging
import os

from nexus.contracts.constants import DEFAULT_OTEL_ENDPOINT

# Re-export tier-neutral utilities so existing server-layer callers
# (e.g. fastapi_server.py) continue to work without import changes.
from nexus.lib.telemetry import (  # noqa: F401
    add_span_attribute,
    get_tracer,
    is_telemetry_enabled,
    record_exception,
)

logger = logging.getLogger(__name__)


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
    from nexus.lib.telemetry import _initialized, init_telemetry_state

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
        _endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTEL_ENDPOINT)
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
            else float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.1"))
        )

        from nexus.server._version import get_nexus_version

        # Create resource with service info
        resource = Resource.create(
            {
                "service.name": _service_name,
                "service.version": get_nexus_version(),
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

        # Store tracer and mark initialized in lib.telemetry
        tracer = trace.get_tracer(__name__)
        init_telemetry_state(True, tracer)

        # Auto-instrument libraries
        _instrument_libraries()

        # Inject rebac tracer into permission tracing module
        from nexus.bricks.rebac.rebac_tracing import set_tracer as _set_rebac_tracer

        _set_rebac_tracer(trace.get_tracer("nexus.bricks.rebac"))

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


def _instrument_libraries() -> None:
    """Auto-instrument supported libraries."""
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

    Args:
        app: FastAPI application instance

    Returns:
        True if instrumented, False if telemetry disabled or error
    """
    from nexus.lib.telemetry import _initialized

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


def shutdown_telemetry() -> None:
    """Shutdown telemetry and flush pending spans."""
    from nexus.lib.telemetry import _initialized, reset_telemetry_state

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
        reset_telemetry_state()
        # Reset rebac tracer
        from nexus.bricks.rebac.rebac_tracing import reset_tracer as _reset_rebac_tracer

        _reset_rebac_tracer()
