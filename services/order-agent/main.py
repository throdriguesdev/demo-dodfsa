"""
Order Simulation Agent — stateful customer journey simulator.

Replaces / complements k6 as the primary traffic generator for the LGTM demo
stack.  Each "persona" walks through a realistic purchase lifecycle so that
traces, logs, and metrics tell an interesting story in Grafana.
"""

import asyncio
import random
import time
import os
import socket

import httpx
import structlog
from prometheus_client import start_http_server, Counter, Histogram, Gauge

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "order-agent"),
        "service.namespace": "lgtm-demo",
        "deployment.environment": "demo",
        "service.version": os.getenv("SERVICE_VERSION", "1.0.0"),
        "service.instance.id": socket.gethostname(),
    }
)

provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(
    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    insecure=True,
)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

HTTPXClientInstrumentor().instrument()

# ---------------------------------------------------------------------------
# Structured logging — pure JSON to stdout with trace context
# ---------------------------------------------------------------------------


def add_trace_context(logger, method_name, event_dict):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        add_trace_context,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.getenv("API_URL", "http://api:8080")
CONCURRENT_PERSONAS = int(os.getenv("ORDER_AGENT_PERSONAS_CONCURRENT", "3"))
CYCLE_DELAY = int(os.getenv("ORDER_AGENT_CYCLE_DELAY_SECONDS", "8"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

JOURNEYS_STARTED = Counter(
    "journeys_started_total", "Journeys started", ["persona"],
)
JOURNEYS_COMPLETED = Counter(
    "journeys_completed_total", "Journeys completed", ["persona"],
)
JOURNEYS_FAILED = Counter(
    "journeys_failed_total", "Journeys failed", ["persona", "reason"],
)
JOURNEY_DURATION = Histogram(
    "journey_duration_seconds",
    "Journey duration",
    ["persona"],
    buckets=[1, 2, 5, 10, 20, 30, 60],
)
ORDERS_VERIFIED = Counter(
    "orders_verified_total",
    "Orders verified",
    ["expected_status", "actual_status"],
)
FULL_LIFECYCLE = Counter(
    "full_lifecycle_completed_total", "Full lifecycle completions",
)
API_ERRORS = Counter(
    "agent_api_errors_total", "API call errors", ["endpoint", "status"],
)
ACTIVE_JOURNEYS = Gauge(
    "active_journeys", "Number of journeys currently in progress",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def safe_request(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    """Execute an HTTP request, returning the response or None on failure."""
    try:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            API_ERRORS.labels(endpoint=url, status=str(resp.status_code)).inc()
            logger.warning(
                "api_error_response",
                method=method,
                url=url,
                status=resp.status_code,
            )
        return resp
    except Exception as exc:
        API_ERRORS.labels(endpoint=url, status="exception").inc()
        logger.warning(
            "api_request_failed",
            method=method,
            url=url,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Persona journeys
# ---------------------------------------------------------------------------


async def regular_buyer_journey(client: httpx.AsyncClient):
    """Browse -> check inventory -> place order -> wait -> verify status & payment."""
    logger.info("journey_step", persona="regular_buyer", step="browse_products")
    await safe_request(client, "GET", "/products")

    logger.info("journey_step", persona="regular_buyer", step="check_inventory")
    await safe_request(client, "GET", "/inventory")

    logger.info("journey_step", persona="regular_buyer", step="place_order")
    resp = await safe_request(client, "POST", "/orders")
    if resp is None or resp.status_code != 200:
        logger.warning("journey_abort", persona="regular_buyer", reason="order_creation_failed")
        return

    order = resp.json()
    order_id = order["id"]
    logger.info("journey_step", persona="regular_buyer", step="waiting_for_processing", order_id=order_id)

    # Give the worker time to process the order
    wait_time = random.uniform(3.0, 8.0)
    await asyncio.sleep(wait_time)

    # Check order status
    logger.info("journey_step", persona="regular_buyer", step="check_status", order_id=order_id)
    status_resp = await safe_request(client, "GET", f"/orders/{order_id}")
    if status_resp is not None and status_resp.status_code == 200:
        actual_status = status_resp.json().get("status", "unknown")
        ORDERS_VERIFIED.labels(expected_status="completed", actual_status=actual_status).inc()
        logger.info(
            "order_status_verified",
            persona="regular_buyer",
            order_id=order_id,
            actual_status=actual_status,
        )

    # Check payment details
    logger.info("journey_step", persona="regular_buyer", step="check_payment", order_id=order_id)
    pay_resp = await safe_request(client, "GET", f"/orders/{order_id}/payment")
    if pay_resp is not None and pay_resp.status_code == 200:
        FULL_LIFECYCLE.inc()
        logger.info(
            "full_lifecycle_complete",
            persona="regular_buyer",
            order_id=order_id,
            payment=pay_resp.json(),
        )


async def power_buyer_journey(client: httpx.AsyncClient):
    """Browse -> bulk order -> check all orders -> check stats."""
    logger.info("journey_step", persona="power_buyer", step="browse_products")
    await safe_request(client, "GET", "/products")

    count = random.randint(3, 5)
    logger.info("journey_step", persona="power_buyer", step="bulk_order", count=count)
    resp = await safe_request(client, "POST", f"/orders/bulk?count={count}")
    if resp is None or resp.status_code != 200:
        logger.warning("journey_abort", persona="power_buyer", reason="bulk_order_failed")
        return

    orders = resp.json().get("orders", [])
    logger.info("journey_step", persona="power_buyer", step="bulk_created", order_count=len(orders))

    # Small pause to let things propagate
    await asyncio.sleep(random.uniform(1.0, 3.0))

    # Check all orders
    logger.info("journey_step", persona="power_buyer", step="list_orders")
    await safe_request(client, "GET", "/orders")

    # Check each created order individually
    for order in orders:
        oid = order.get("id")
        if oid:
            await safe_request(client, "GET", f"/orders/{oid}")
            await asyncio.sleep(random.uniform(0.2, 0.5))

    # Check stats
    logger.info("journey_step", persona="power_buyer", step="check_stats")
    await safe_request(client, "GET", "/stats")


async def window_shopper_journey(client: httpx.AsyncClient):
    """Browse products and stats, never buys anything."""
    logger.info("journey_step", persona="window_shopper", step="browse_products")
    await safe_request(client, "GET", "/products")

    await asyncio.sleep(random.uniform(1.0, 3.0))

    logger.info("journey_step", persona="window_shopper", step="check_inventory")
    await safe_request(client, "GET", "/inventory")

    await asyncio.sleep(random.uniform(0.5, 2.0))

    logger.info("journey_step", persona="window_shopper", step="check_stats")
    await safe_request(client, "GET", "/stats")

    logger.info("journey_step", persona="window_shopper", step="check_product_stats")
    await safe_request(client, "GET", "/stats/products")


async def impatient_buyer_journey(client: httpx.AsyncClient):
    """Place order -> check status immediately -> check again -> cancel if pending."""
    logger.info("journey_step", persona="impatient_buyer", step="place_order")
    resp = await safe_request(client, "POST", "/orders")
    if resp is None or resp.status_code != 200:
        logger.warning("journey_abort", persona="impatient_buyer", reason="order_creation_failed")
        return

    order = resp.json()
    order_id = order["id"]

    # Immediately check status (impatient!)
    logger.info("journey_step", persona="impatient_buyer", step="immediate_check", order_id=order_id)
    status_resp = await safe_request(client, "GET", f"/orders/{order_id}")

    first_status = "unknown"
    if status_resp is not None and status_resp.status_code == 200:
        first_status = status_resp.json().get("status", "unknown")
        logger.info(
            "impatient_first_check",
            order_id=order_id,
            status=first_status,
        )

    # Wait just a tiny bit, then check again
    await asyncio.sleep(random.uniform(1.0, 2.0))

    logger.info("journey_step", persona="impatient_buyer", step="second_check", order_id=order_id)
    status_resp2 = await safe_request(client, "GET", f"/orders/{order_id}")

    second_status = "unknown"
    if status_resp2 is not None and status_resp2.status_code == 200:
        second_status = status_resp2.json().get("status", "unknown")
        logger.info(
            "impatient_second_check",
            order_id=order_id,
            status=second_status,
        )

    ORDERS_VERIFIED.labels(expected_status="any", actual_status=second_status).inc()

    # If still pending after two checks, try to cancel
    if second_status == "pending":
        logger.info("journey_step", persona="impatient_buyer", step="cancel_order", order_id=order_id)
        cancel_resp = await safe_request(client, "POST", f"/orders/{order_id}/cancel")
        if cancel_resp is not None and cancel_resp.status_code == 200:
            logger.info("order_cancelled_by_impatient", order_id=order_id)
        else:
            logger.info(
                "cancel_failed_or_already_processed",
                order_id=order_id,
                status=cancel_resp.status_code if cancel_resp else "no_response",
            )


async def suspicious_buyer_journey(client: httpx.AsyncClient):
    """Rapid-fire 5-8 orders with minimal delay (triggers fraud velocity checks)."""
    order_count = random.randint(5, 8)
    logger.info(
        "journey_step",
        persona="suspicious_buyer",
        step="rapid_fire_start",
        planned_orders=order_count,
    )

    order_ids = []
    for i in range(order_count):
        resp = await safe_request(client, "POST", "/orders")
        if resp is not None and resp.status_code == 200:
            oid = resp.json().get("id")
            order_ids.append(oid)
            logger.info(
                "suspicious_rapid_order",
                order_number=i + 1,
                order_id=oid,
            )
        # Minimal delay between orders to trigger velocity detection
        await asyncio.sleep(random.uniform(0.1, 0.5))

    logger.info(
        "journey_step",
        persona="suspicious_buyer",
        step="rapid_fire_complete",
        created_count=len(order_ids),
    )


async def analytics_user_journey(client: httpx.AsyncClient):
    """Only hits stats endpoints, simulates a dashboard or reporting user."""
    logger.info("journey_step", persona="analytics_user", step="check_general_stats")
    await safe_request(client, "GET", "/stats")

    await asyncio.sleep(random.uniform(0.5, 1.5))

    logger.info("journey_step", persona="analytics_user", step="check_product_stats")
    await safe_request(client, "GET", "/stats/products")

    await asyncio.sleep(random.uniform(0.5, 1.5))

    logger.info("journey_step", persona="analytics_user", step="check_payment_stats")
    await safe_request(client, "GET", "/stats/payments")


# ---------------------------------------------------------------------------
# Persona selector and main loop
# ---------------------------------------------------------------------------

PERSONAS = [
    ("regular_buyer", 40, regular_buyer_journey),
    ("power_buyer", 15, power_buyer_journey),
    ("window_shopper", 20, window_shopper_journey),
    ("impatient_buyer", 10, impatient_buyer_journey),
    ("suspicious_buyer", 5, suspicious_buyer_journey),
    ("analytics_user", 10, analytics_user_journey),
]
WEIGHTS = [p[1] for p in PERSONAS]


async def run_persona(client: httpx.AsyncClient):
    """Continuously pick a random persona and execute its journey."""
    while True:
        chosen = random.choices(PERSONAS, weights=WEIGHTS, k=1)[0]
        name, _, journey_fn = chosen

        JOURNEYS_STARTED.labels(persona=name).inc()
        ACTIVE_JOURNEYS.inc()
        start = time.monotonic()

        try:
            with tracer.start_as_current_span(
                f"{name}_journey",
                attributes={"customer.persona": name},
            ):
                await journey_fn(client)
            JOURNEYS_COMPLETED.labels(persona=name).inc()
        except Exception as exc:
            JOURNEYS_FAILED.labels(persona=name, reason=type(exc).__name__).inc()
            logger.error("journey_failed", persona=name, error=str(exc))
        finally:
            elapsed = time.monotonic() - start
            JOURNEY_DURATION.labels(persona=name).observe(elapsed)
            ACTIVE_JOURNEYS.dec()

        # Jitter the delay so personas don't synchronize
        delay = random.uniform(CYCLE_DELAY * 0.5, CYCLE_DELAY * 1.5)
        await asyncio.sleep(delay)


async def main():
    logger.info(
        "agent_starting",
        api_url=API_URL,
        concurrent=CONCURRENT_PERSONAS,
        cycle_delay=CYCLE_DELAY,
    )

    # Give the rest of the stack time to come up
    await asyncio.sleep(15)

    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        # Wait for the API to become healthy
        for attempt in range(30):
            try:
                resp = await client.get("/health")
                if resp.status_code == 200:
                    logger.info("api_ready")
                    break
            except Exception:
                pass
            logger.warning("api_not_ready", attempt=attempt + 1)
            await asyncio.sleep(3)
        else:
            logger.error("api_never_became_ready")
            return

        # Spawn concurrent persona runners
        tasks = [
            asyncio.create_task(run_persona(client))
            for _ in range(CONCURRENT_PERSONAS)
        ]
        logger.info("personas_launched", count=CONCURRENT_PERSONAS)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    start_http_server(9096)
    logger.info("metrics_server_started", port=9096)
    asyncio.run(main())
