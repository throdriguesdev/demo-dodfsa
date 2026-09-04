import logging
import os
import random
import socket
import sys
import time

import psycopg2
import structlog
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "notification-service"),
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

Psycopg2Instrumentor().instrument(
    skip_dep_check=True,
    enable_commenter=True,
    commenter_options={"db_driver": True, "opentelemetry_values": True},
)

# ---------------------------------------------------------------------------
# Structured logging — pure JSON to stdout
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
logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

# ---------------------------------------------------------------------------
# Custom Prometheus metrics
# ---------------------------------------------------------------------------

def _trace_exemplar():
    """Return current trace_id as exemplar for metric->trace correlation."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return {}


NOTIFICATIONS_LATENCY = Histogram(
    "notifications_latency_seconds",
    "Notification sending latency by channel",
    ["channel"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0],
)

NOTIFICATIONS_BY_CHANNEL = Counter(
    "notifications_by_channel",
    "Notifications processed by channel and status",
    ["channel", "status"],
)

NOTIFICATION_FAILURES = Counter(
    "notification_failures_total",
    "Total notification failures by channel",
    ["channel"],
)

# ---------------------------------------------------------------------------
# Channel-specific simulated latency (seconds)
# ---------------------------------------------------------------------------
CHANNEL_LATENCY = {
    "sms": 0.5,
    "email": 0.2,
    "push": 0.05,
}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="LGTM Demo Notification Service")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
Instrumentator().instrument(app).expose(app)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/appdb")


def get_db():
    return psycopg2.connect(DATABASE_URL)


@app.on_event("startup")
def startup():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            order_id INT NOT NULL,
            channel VARCHAR(20) NOT NULL,
            message TEXT NOT NULL,
            status VARCHAR(20) DEFAULT 'sent',
            created_at TIMESTAMP DEFAULT NOW()
        );
        DO $$ BEGIN
            ALTER TABLE notifications ADD COLUMN status VARCHAR(20) DEFAULT 'sent';
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("database_initialized", service="notification-service")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "healthy", "service": "notification-service"}


@app.post("/notify")
def notify(order_id: int, status: str, channel: str = "email", message: str = ""):
    if channel not in CHANNEL_LATENCY:
        raise HTTPException(status_code=400, detail=f"Unsupported channel: {channel}. Use: {list(CHANNEL_LATENCY.keys())}")

    if not message:
        message = f"Order {order_id} is now {status}"

    with tracer.start_as_current_span(
        "send_notification",
        attributes={
            "notification.channel": channel,
            "notification.order_id": order_id,
            "notification.status": status,
        },
    ):
        # Simulate channel-specific sending latency with slight jitter
        base_latency = CHANNEL_LATENCY[channel]
        jitter = random.uniform(-0.02, 0.02)
        delay = max(0.01, base_latency + jitter)

        start = time.perf_counter()
        time.sleep(delay)
        elapsed = time.perf_counter() - start

        # Simulate occasional failures (~5% chance)
        failed = random.random() < 0.05
        notification_status = "failed" if failed else "sent"

        # Persist notification record
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO notifications (order_id, channel, message, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (order_id, channel, message, notification_status),
        )
        notif_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # Record metrics
        NOTIFICATIONS_LATENCY.labels(channel=channel).observe(elapsed, exemplar=_trace_exemplar())
        NOTIFICATIONS_BY_CHANNEL.labels(channel=channel, status=notification_status).inc(exemplar=_trace_exemplar())

        if failed:
            NOTIFICATION_FAILURES.labels(channel=channel).inc(exemplar=_trace_exemplar())
            logger.warning(
                "notification_failed",
                notification_id=notif_id,
                order_id=order_id,
                channel=channel,
                status=status,
                latency=round(elapsed, 4),
            )
        else:
            logger.info(
                "notification_sent",
                notification_id=notif_id,
                order_id=order_id,
                channel=channel,
                status=status,
                latency=round(elapsed, 4),
            )

    return {
        "notification_id": notif_id,
        "order_id": order_id,
        "channel": channel,
        "status": notification_status,
        "latency": round(elapsed, 4),
    }


@app.get("/notifications/{order_id}")
def get_notifications(order_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, order_id, channel, message, status, created_at FROM notifications WHERE order_id = %s ORDER BY id DESC",
        (order_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No notifications found for order {order_id}")

    return {
        "order_id": order_id,
        "notifications": [
            {
                "id": r[0],
                "order_id": r[1],
                "channel": r[2],
                "message": r[3],
                "status": r[4],
                "created_at": str(r[5]),
            }
            for r in rows
        ],
    }
