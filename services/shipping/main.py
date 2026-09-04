import json
import os
import random
import socket
import string
import time

import pika
import psycopg2
import structlog
from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "shipping-service"),
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
# Structured logging
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
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/appdb")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

# ---------------------------------------------------------------------------
# Carrier definitions
# ---------------------------------------------------------------------------
CARRIERS = {
    "correios": {"latency_ms": 800, "cost_min": 15.0, "cost_max": 25.0, "days_min": 5, "days_max": 7},
    "sedex":    {"latency_ms": 200, "cost_min": 25.0, "cost_max": 45.0, "days_min": 1, "days_max": 3},
    "jadlog":   {"latency_ms": 500, "cost_min": 20.0, "cost_max": 35.0, "days_min": 3, "days_max": 5},
}


def get_db():
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
from prometheus_client import start_http_server, Counter, Histogram

SHIPPING_PROCESSED = Counter(
    "shipping_processed_total", "Shipments processed", ["carrier"],
)
SHIPPING_LATENCY = Histogram(
    "shipping_latency_seconds", "Shipping processing latency per carrier", ["carrier"],
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0],
)
SHIPPING_COST = Counter(
    "shipping_cost_total", "Total shipping cost by carrier (BRL)", ["carrier"],
)
SHIPPING_ERRORS = Counter(
    "shipping_errors_total", "Shipping processing errors",
)


# ---------------------------------------------------------------------------
# DB initialization
# ---------------------------------------------------------------------------
def init_db():
    """Create shipments table and add shipping columns to orders if needed."""
    for attempt in range(15):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shipments (
                    id SERIAL PRIMARY KEY,
                    order_id INT NOT NULL,
                    carrier VARCHAR(30) NOT NULL,
                    tracking_code VARCHAR(50),
                    shipping_cost NUMERIC(10,2),
                    estimated_days INT,
                    status VARCHAR(20) DEFAULT 'dispatched',
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE orders ADD COLUMN shipping_status VARCHAR(20) DEFAULT NULL;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$;
            """)
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE orders ADD COLUMN tracking_code VARCHAR(50) DEFAULT NULL;
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$;
            """)
            conn.commit()
            cur.close()
            conn.close()
            logger.info("db_initialized")
            return
        except Exception as e:
            logger.warning("db_init_retry", attempt=attempt + 1, error=str(e))
            time.sleep(3)
    raise RuntimeError("Failed to initialize database after retries")


# ---------------------------------------------------------------------------
# Processing steps
# ---------------------------------------------------------------------------

def calculate_shipping(order_id, product, total):
    """Look up carrier costs and return the full cost table."""
    with tracer.start_as_current_span(
        "calculate_shipping",
        attributes={"order.id": order_id, "product": product, "order.total": total},
    ) as span:
        costs = {}
        for name, cfg in CARRIERS.items():
            cost = round(random.uniform(cfg["cost_min"], cfg["cost_max"]), 2)
            days = random.randint(cfg["days_min"], cfg["days_max"])
            costs[name] = {"cost": cost, "days": days}
        span.set_attribute("shipping.options_count", len(costs))
        logger.info("shipping_calculated", order_id=order_id, options=costs)
        return costs


def assign_carrier(order_id, costs):
    """Randomly select a carrier and simulate its processing latency."""
    carrier_name = random.choice(list(costs.keys()))
    cfg = CARRIERS[carrier_name]
    chosen = costs[carrier_name]

    with tracer.start_as_current_span(
        "assign_carrier",
        attributes={
            "order.id": order_id,
            "shipping.carrier": carrier_name,
            "shipping.cost": chosen["cost"],
            "shipping.estimated_days": chosen["days"],
        },
    ) as span:
        # Simulate carrier API latency
        latency_s = cfg["latency_ms"] / 1000.0
        time.sleep(latency_s)
        span.set_attribute("shipping.latency_ms", cfg["latency_ms"])
        logger.info(
            "carrier_assigned",
            order_id=order_id,
            carrier=carrier_name,
            cost=chosen["cost"],
            days=chosen["days"],
            latency_ms=cfg["latency_ms"],
        )
        return carrier_name, chosen["cost"], chosen["days"]


def generate_tracking(order_id, carrier):
    """Generate a fake tracking code."""
    with tracer.start_as_current_span(
        "generate_tracking",
        attributes={"order.id": order_id, "shipping.carrier": carrier},
    ) as span:
        digits = "".join(random.choices(string.digits, k=13))
        tracking_code = f"BR{digits}"
        span.set_attribute("shipping.tracking_code", tracking_code)
        logger.info("tracking_generated", order_id=order_id, tracking_code=tracking_code)
        return tracking_code


def update_order_shipping(order_id, tracking_code):
    """Update the orders table with shipping status and tracking code."""
    with tracer.start_as_current_span(
        "update_order",
        attributes={"order.id": order_id, "shipping.tracking_code": tracking_code},
    ):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET shipping_status = %s, tracking_code = %s WHERE id = %s",
            ("dispatched", tracking_code, order_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("order_shipping_updated", order_id=order_id, status="dispatched")


def record_shipment(order_id, carrier, tracking_code, shipping_cost, estimated_days):
    """Insert a row into the shipments table."""
    with tracer.start_as_current_span(
        "record_shipment",
        attributes={
            "order.id": order_id,
            "shipping.carrier": carrier,
            "shipping.tracking_code": tracking_code,
            "shipping.cost": shipping_cost,
            "shipping.estimated_days": estimated_days,
        },
    ) as span:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO shipments (order_id, carrier, tracking_code, shipping_cost, estimated_days, status)
               VALUES (%s, %s, %s, %s, %s, 'dispatched') RETURNING id""",
            (order_id, carrier, tracking_code, shipping_cost, estimated_days),
        )
        shipment_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        span.set_attribute("shipment.id", shipment_id)
        logger.info("shipment_recorded", shipment_id=shipment_id, order_id=order_id)
        return shipment_id


def publish_notification(channel, order_id, product, tracking_code, carrier):
    """Publish shipping_dispatched event to the notifications exchange."""
    with tracer.start_as_current_span(
        "notifications publish",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.destination.name": "notifications",
            "messaging.operation": "publish",
            "messaging.rabbitmq.routing_key": "notifications.shipping_dispatched",
            "order.id": order_id,
        },
    ):
        headers = {}
        propagate.inject(carrier=headers)

        body = json.dumps({
            "order_id": order_id,
            "product": product,
            "tracking_code": tracking_code,
            "carrier": carrier,
            "status": "shipped",
        })

        channel.basic_publish(
            exchange="notifications",
            routing_key="notifications.shipping_dispatched",
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
                headers=headers,
            ),
        )
        logger.info(
            "notification_published",
            order_id=order_id,
            routing_key="notifications.shipping_dispatched",
        )


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

def process_shipment(ch, method, properties, body):
    ctx = propagate.extract(carrier=properties.headers or {})
    start_time = time.monotonic()

    with tracer.start_as_current_span(
        "shipping process",
        context=ctx,
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.source.name": "orders.completed",
            "messaging.operation": "process",
            "messaging.consumer.group.name": "shipping-service",
        },
    ) as span:
        try:
            msg = json.loads(body)
            order_id = msg["order_id"]
            product = msg["product"]
            total = msg.get("total", 0)

            span.set_attribute("order.id", order_id)
            span.set_attribute("messaging.message.conversation_id", str(order_id))

            logger.info("shipment_received", order_id=order_id, product=product, total=total)

            # Step 1: Calculate shipping options
            costs = calculate_shipping(order_id, product, total)

            # Step 2: Assign carrier (with simulated latency)
            carrier_name, shipping_cost, estimated_days = assign_carrier(order_id, costs)

            # Step 3: Generate tracking code
            tracking_code = generate_tracking(order_id, carrier_name)

            # Step 4: Update order with shipping info
            update_order_shipping(order_id, tracking_code)

            # Step 5: Record shipment in shipments table
            shipment_id = record_shipment(
                order_id, carrier_name, tracking_code, shipping_cost, estimated_days,
            )

            # Step 6: Publish notification
            publish_notification(ch, order_id, product, tracking_code, carrier_name)

            # Record metrics
            duration = time.monotonic() - start_time
            SHIPPING_PROCESSED.labels(carrier=carrier_name).inc()
            SHIPPING_LATENCY.labels(carrier=carrier_name).observe(duration)
            SHIPPING_COST.labels(carrier=carrier_name).inc(shipping_cost)
            SHIPPING_ERRORS  # reference to keep linter quiet; no increment on success

            span.set_attribute("shipping.carrier", carrier_name)
            span.set_attribute("shipping.tracking_code", tracking_code)
            span.set_attribute("shipping.cost", shipping_cost)
            span.set_attribute("shipping.estimated_days", estimated_days)
            span.set_attribute("shipment.id", shipment_id)

            logger.info(
                "shipment_completed",
                order_id=order_id,
                carrier=carrier_name,
                tracking_code=tracking_code,
                shipping_cost=shipping_cost,
                estimated_days=estimated_days,
                duration=round(duration, 3),
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            span.set_status(StatusCode.ERROR, str(e))
            span.record_exception(e)
            SHIPPING_ERRORS.inc()
            logger.error("shipment_failed", error=str(e), error_type=type(e).__name__)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


# ---------------------------------------------------------------------------
# RabbitMQ connection with retries
# ---------------------------------------------------------------------------

def connect_rabbitmq(max_retries=10, delay=3):
    for attempt in range(1, max_retries + 1):
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            params.heartbeat = 600
            params.blocked_connection_timeout = 300
            conn = pika.BlockingConnection(params)
            logger.info("rabbitmq_connected", attempt=attempt)
            return conn
        except pika.exceptions.AMQPConnectionError:
            logger.warning("rabbitmq_retry", attempt=attempt)
            time.sleep(delay)
    raise RuntimeError("Failed to connect to RabbitMQ")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("shipping_service_starting")

    # Start Prometheus metrics server
    start_http_server(9095)
    logger.info("metrics_server_started", port=9095)

    # Initialize database tables
    init_db()

    # Connect to RabbitMQ
    conn = connect_rabbitmq()
    channel = conn.channel()

    # Declare consume topology
    channel.exchange_declare(exchange="orders.completed", exchange_type="topic", durable=True)
    channel.queue_declare(
        queue="shipping.queue",
        durable=True,
        arguments={"x-dead-letter-exchange": "shipping.dlx"},
    )
    channel.queue_bind(queue="shipping.queue", exchange="orders.completed", routing_key="orders.completed")

    # Declare DLQ
    channel.exchange_declare(exchange="shipping.dlx", exchange_type="fanout", durable=True)
    channel.queue_declare(queue="shipping.dlq", durable=True)
    channel.queue_bind(queue="shipping.dlq", exchange="shipping.dlx")

    # Declare publish target
    channel.exchange_declare(exchange="notifications", exchange_type="topic", durable=True)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue="shipping.queue", on_message_callback=process_shipment)

    logger.info("shipping_service_ready", queue="shipping.queue")
    channel.start_consuming()


if __name__ == "__main__":
    main()
