import json
import os
import random
import socket
import time
from contextlib import contextmanager

import pika
import psycopg2
import redis
import requests
import structlog
from opentelemetry import baggage, context, propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, SpanContext, SpanKind, StatusCode, TraceFlags

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "order-worker"),
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
RedisInstrumentor().instrument()
RequestsInstrumentor().instrument()

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
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
API_URL = os.getenv("API_URL", "http://api:8080")
FRAUD_URL = os.getenv("FRAUD_URL", "http://fraud-service:9094")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:9093")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-gateway:9092")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://notification-service:9091")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def get_redis():
    return redis.from_url(REDIS_URL)


# ---------------------------------------------------------------------------
# Prometheus metrics (scraped by Alloy on port 9090)
# ---------------------------------------------------------------------------
from prometheus_client import start_http_server, Counter, Histogram

WORKER_MESSAGES_PROCESSED = Counter(
    "worker_messages_processed_total", "Messages processed", ["status"],
)
WORKER_STEP_DURATION = Histogram(
    "worker_step_duration_seconds", "Duration per processing step", ["step"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)
WORKER_PROCESSING_DURATION = Histogram(
    "worker_processing_duration_seconds", "Total message processing time",
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10],
)


@contextmanager
def timed_step(step_name):
    start = time.monotonic()
    try:
        yield
    finally:
        WORKER_STEP_DURATION.labels(step=step_name).observe(time.monotonic() - start)


# ---------------------------------------------------------------------------
# Processing steps — now using microservices
# ---------------------------------------------------------------------------

def call_fraud_service(order_id, product, quantity, amount):
    """Call fraud-service to analyze order risk."""
    with tracer.start_as_current_span("call_fraud_service", attributes={
        "order_id": order_id, "product": product, "amount": amount,
    }) as span:
        resp = requests.post(
            f"{FRAUD_URL}/analyze",
            json={"order_id": order_id, "product": product, "quantity": quantity, "amount": amount},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        span.set_attribute("fraud.score", result.get("score", 0))
        span.set_attribute("fraud.decision", result.get("decision", "unknown"))
        logger.info("fraud_check_done", order_id=order_id, score=result.get("score"), decision=result.get("decision"))
        return result


def reserve_inventory(order_id, product, quantity):
    """Call inventory-service to reserve stock."""
    with tracer.start_as_current_span("reserve_inventory", attributes={
        "order_id": order_id, "product": product, "quantity": quantity,
    }) as span:
        resp = requests.post(
            f"{INVENTORY_URL}/reserve",
            json={"order_id": order_id, "product": product, "quantity": quantity},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        span.set_attribute("inventory.status", result.get("status", "unknown"))
        logger.info("inventory_reserved", order_id=order_id, product=product)
        return result


def confirm_inventory(order_id):
    """Call inventory-service to confirm reservation."""
    with tracer.start_as_current_span("confirm_inventory", attributes={"order_id": order_id}):
        resp = requests.post(
            f"{INVENTORY_URL}/confirm",
            json={"order_id": order_id},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("inventory_confirmed", order_id=order_id)


def rollback_inventory(order_id, product, quantity):
    """Call inventory-service to rollback reservation."""
    with tracer.start_as_current_span("rollback_inventory", attributes={"order_id": order_id}):
        resp = requests.post(
            f"{INVENTORY_URL}/rollback",
            json={"order_id": order_id, "product": product, "quantity": quantity},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("inventory_rolled_back", order_id=order_id, product=product)


def process_payment(order_id, amount):
    """Call payment-gateway to process payment."""
    with tracer.start_as_current_span("process_payment", attributes={
        "order_id": order_id, "amount": amount,
    }) as span:
        methods = ["credit_card", "debit_card", "pix"]
        method = random.choice(methods)
        span.set_attribute("payment.method", method)

        # First attempt in its own span (for span link if retry needed)
        first_attempt_ctx = None
        with tracer.start_as_current_span("payment_attempt", attributes={
            "payment.method": method, "payment.attempt": 1,
        }) as attempt_span:
            resp = requests.post(
                f"{PAYMENT_URL}/process",
                json={"order_id": order_id, "amount": amount, "method": method},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            attempt_span.set_attribute("payment.status", result.get("status", "unknown"))
            first_attempt_ctx = attempt_span.get_span_context()

        if result.get("status") == "declined":
            # --- 2.1 Span Event: payment_declined ---
            span.add_event("payment_declined", {
                "method": method, "reason": result.get("decline_reason", ""),
                "attempt": 1,
            })
            logger.warning("payment_declined_retrying", order_id=order_id, method=method)

            # --- 2.2 Span Link: retry links to first attempt ---
            retry_method = random.choice([m for m in methods if m != method])
            link = Link(first_attempt_ctx, {"link.type": "retry_of"})
            with tracer.start_as_current_span("payment_retry", links=[link], attributes={
                "payment.method": retry_method, "payment.attempt": 2,
            }) as retry_span:
                resp = requests.post(
                    f"{PAYMENT_URL}/process",
                    json={"order_id": order_id, "amount": amount, "method": retry_method},
                    timeout=15,
                )
                resp.raise_for_status()
                result = resp.json()
                method = retry_method
                retry_span.set_attribute("payment.status", result.get("status", "unknown"))

        if result.get("status") == "declined":
            span.add_event("payment_declined", {
                "method": method, "reason": result.get("decline_reason", ""),
                "attempt": 2,
            })
            raise ValueError(f"Payment declined after retry: {result.get('decline_reason', 'unknown')}")

        # --- 2.1 Span Event: payment_authorized ---
        span.add_event("payment_authorized", {
            "method": method, "amount": amount,
            "transaction_id": result.get("transaction_id", 0),
        })
        logger.info("payment_processed", order_id=order_id, method=method, status=result["status"])
        return result.get("transaction_id"), method


def send_notification(order_id, status, message=None):
    """Call notification-service to send notification."""
    with tracer.start_as_current_span("send_notification", attributes={"order_id": order_id, "status": status}):
        channels = ["email", "push"]
        channel = random.choice(channels)
        resp = requests.post(
            f"{NOTIFICATION_URL}/notify",
            params={
                "order_id": order_id,
                "status": status,
                "channel": channel,
                "message": message or f"Order {order_id} is now {status}",
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("notification_sent", order_id=order_id, status=status, channel=channel)


def calculate_total(product, quantity):
    """Calculate order total from Redis product catalog."""
    with tracer.start_as_current_span("calculate_total", attributes={"product": product}) as span:
        r = get_redis()
        price = r.hget(f"product:{product}", "price")
        unit_price = float(price) if price else 0
        total = round(unit_price * quantity, 2)
        span.set_attribute("unit_price", unit_price)
        span.set_attribute("total", total)
        logger.info("total_calculated", product=product, unit_price=unit_price, quantity=quantity, total=total)
        return unit_price, total


def update_order(order_id, status, total=None):
    """Update order status in Postgres with explicit transaction."""
    with tracer.start_as_current_span("update_order", attributes={"order_id": order_id, "status": status}):
        conn = get_db()
        cur = conn.cursor()
        if total is not None:
            cur.execute("UPDATE orders SET status = %s, total = %s WHERE id = %s", (status, total, order_id))
        else:
            cur.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
        conn.commit()
        cur.close()
        conn.close()
        logger.info("order_updated", order_id=order_id, status=status)


def write_order_event(order_id, event, service, data=None):
    """Write an event to the order_events audit log."""
    with tracer.start_as_current_span("write_order_event", attributes={"order_id": order_id, "event": event}):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO order_events (order_id, event, service, data, trace_id) VALUES (%s, %s, %s, %s, %s)",
            (order_id, event, service, json.dumps(data or {}), trace_id),
        )
        conn.commit()
        cur.close()
        conn.close()


def report_completed(order_id, product, total, method, duration):
    """Report business metrics to the API (where Prometheus scrapes them)."""
    with tracer.start_as_current_span("report_metrics", attributes={"order_id": order_id}):
        requests.post(
            f"{API_URL}/webhooks/order-completed",
            params={"order_id": order_id, "product": product, "total": total, "method": method, "duration": duration},
            timeout=5,
        )


def report_failed(order_id, product, reason):
    with tracer.start_as_current_span("report_metrics", attributes={"order_id": order_id}):
        requests.post(
            f"{API_URL}/webhooks/order-failed",
            params={"order_id": order_id, "product": product, "reason": reason},
            timeout=5,
        )


def update_cache(order_id, product, quantity, status, total):
    with tracer.start_as_current_span("update_cache", attributes={"order_id": order_id}):
        r = get_redis()
        r.setex(f"order:{order_id}", 300, json.dumps({
            "id": order_id, "product": product, "quantity": quantity,
            "total": total, "status": status,
        }))


def publish_order_completed(ch, order_id, product, total, method, customer_name="anonymous"):
    """Publish to orders.completed exchange for shipping service."""
    with tracer.start_as_current_span(
        "orders.completed publish", kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.destination.name": "orders.completed",
            "messaging.rabbitmq.routing_key": "orders.completed",
            "messaging.message.conversation_id": str(order_id),
            "messaging.operation": "publish",
        },
    ):
        headers = {}
        propagate.inject(carrier=headers)
        body_json = json.dumps({
            "order_id": order_id, "product": product, "total": total, "method": method,
            "customer_name": customer_name,
        })
        ch.basic_publish(
            exchange="orders.completed",
            routing_key="orders.completed",
            body=body_json,
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2, headers=headers,
            ),
        )
        logger.info("order_completed_published", order_id=order_id)


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

def process_order(ch, method, properties, body):
    ctx = propagate.extract(carrier=properties.headers or {})
    start_time = time.monotonic()

    # Parse message and build span links before span creation (links are immutable at creation)
    # but keep it minimal so errors inside the span are still captured
    msg = json.loads(body)
    order_id = msg["order_id"]
    product = msg["product"]
    quantity = msg["quantity"]
    customer_name = msg.get("customer_name") or "anonymous"

    # --- 2.2 Span Links: link to bulk parent if this order came from /orders/bulk ---
    links = []
    if msg.get("bulk_trace_id"):
        bulk_span_ctx = SpanContext(
            trace_id=int(msg["bulk_trace_id"], 16),
            span_id=int(msg["bulk_span_id"], 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        links.append(Link(bulk_span_ctx, {"link.type": "bulk_parent"}))

    with tracer.start_as_current_span(
        "orders process", context=ctx, kind=SpanKind.CONSUMER,
        links=links,
        attributes={
            "messaging.system": "rabbitmq",
            "messaging.operation": "process",
            "order.id": order_id,
        },
    ) as span:
        span.set_attribute("messaging.message.conversation_id", str(order_id))
        span.set_attribute("messaging.consumer.group.name", "order-worker")

        # --- 2.3 Baggage: read business context propagated from API ---
        order_priority = baggage.get_baggage("order_priority", ctx) or "normal"
        customer_tier = baggage.get_baggage("customer_tier", ctx) or "free"
        customer_name_baggage = baggage.get_baggage("customer_name", ctx) or ""
        if customer_name == "anonymous" and customer_name_baggage:
            customer_name = customer_name_baggage
        span.set_attribute("order.priority", order_priority)
        span.set_attribute("customer.tier", customer_tier)
        span.set_attribute("customer.name", customer_name)
        structlog.contextvars.bind_contextvars(customer_name=customer_name)

        try:
            logger.info("order_received", order_id=order_id, product=product, quantity=quantity,
                         priority=order_priority, customer_tier=customer_tier, customer_name=customer_name)

            # Step 1: Calculate total
            with timed_step("calculate_total"):
                unit_price, total = calculate_total(product, quantity)

            # Step 2: Fraud check (before inventory/payment)
            with timed_step("fraud_check"):
                fraud_result = call_fraud_service(order_id, product, quantity, total)

            fraud_decision = fraud_result.get("decision", "approved")
            span.set_attribute("fraud.decision", fraud_decision)

            if fraud_decision == "rejected":
                span.add_event("order_rejected_by_fraud", {"score": fraud_result.get("score", 0)})
                update_order(order_id, "rejected")
                write_order_event(order_id, "fraud_rejected", "order-worker", fraud_result)
                send_notification(order_id, "rejected", f"Order {order_id} rejected by fraud check")
                report_failed(order_id, product, "fraud_rejected")
                WORKER_MESSAGES_PROCESSED.labels(status="rejected").inc()
                logger.warning("order_rejected_fraud", order_id=order_id, score=fraud_result.get("score"))
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            if fraud_decision == "review":
                span.add_event("order_sent_to_review", {"score": fraud_result.get("score", 0)})
                update_order(order_id, "review")
                write_order_event(order_id, "fraud_review", "order-worker", fraud_result)
                send_notification(order_id, "review", f"Order {order_id} flagged for manual review")
                WORKER_MESSAGES_PROCESSED.labels(status="review").inc()
                logger.warning("order_review_fraud", order_id=order_id, score=fraud_result.get("score"))
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            # Step 3: Reserve inventory
            with timed_step("reserve_inventory"):
                inv_result = reserve_inventory(order_id, product, quantity)
            write_order_event(order_id, "inventory_reserved", "order-worker", {"product": product, "quantity": quantity})
            # --- 2.1 Span Event: inventory_reserved ---
            span.add_event("inventory_reserved", {
                "product": product, "quantity": quantity,
                "reservation_id": inv_result.get("reservation_id", "") if isinstance(inv_result, dict) else "",
            })

            # Step 4: Update order to processing
            with timed_step("update_order_processing"):
                update_order(order_id, "processing")

            # Step 5: Process payment via payment gateway
            with timed_step("process_payment"):
                transaction_id, pay_method = process_payment(order_id, total)
            write_order_event(order_id, "payment_processed", "order-worker", {"method": pay_method, "amount": total})

            # Step 6: Confirm inventory reservation
            with timed_step("confirm_inventory"):
                confirm_inventory(order_id)

            # Step 7: Complete order
            with timed_step("update_order_completed"):
                update_order(order_id, "completed", total=total)
            write_order_event(order_id, "order_completed", "order-worker", {"total": total})

            # Step 8: Send notification
            with timed_step("send_notification"):
                send_notification(order_id, "completed")
            # --- 2.1 Span Event: notification_sent ---
            span.add_event("notification_sent", {"order_id": order_id, "status": "completed"})

            # Step 9: Report metrics to API
            duration = time.monotonic() - start_time
            with timed_step("report_completed"):
                report_completed(order_id, product, total, pay_method, duration)

            # Step 10: Update cache
            with timed_step("update_cache"):
                update_cache(order_id, product, quantity, "completed", total)

            # Step 11: Publish to orders.completed for shipping service
            with timed_step("publish_order_completed"):
                publish_order_completed(ch, order_id, product, total, pay_method, customer_name)

            # --- 2.1 Span Event: order_completed ---
            span.add_event("order_completed", {
                "order_id": order_id, "total": total,
                "duration_ms": round((time.monotonic() - start_time) * 1000),
                "payment_method": pay_method, "priority": order_priority,
            })

            WORKER_PROCESSING_DURATION.observe(duration)
            WORKER_MESSAGES_PROCESSED.labels(status="completed").inc()
            logger.info("order_completed", order_id=order_id, total=total, duration=round(duration, 3))
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            span.set_status(StatusCode.ERROR, str(e))
            span.record_exception(e)
            reason = type(e).__name__
            WORKER_MESSAGES_PROCESSED.labels(status="failed").inc()
            logger.error("order_failed", order_id=order_id, error=str(e), reason=reason)
            try:
                update_order(order_id, "failed")
                rollback_inventory(order_id, product, quantity)
                send_notification(order_id, "failed")
                report_failed(order_id, product, reason)
                write_order_event(order_id, "order_failed", "order-worker", {"reason": str(e)})
            except Exception:
                pass
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


# ---------------------------------------------------------------------------
# Main
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


def main():
    logger.info("worker_starting")
    start_http_server(9090)
    logger.info("metrics_server_started", port=9090)
    conn = connect_rabbitmq()
    channel = conn.channel()

    # Orders queue (existing)
    channel.exchange_declare(exchange="orders.dlx", exchange_type="fanout", durable=True)
    channel.queue_declare(queue="orders.dlq", durable=True)
    channel.queue_bind(queue="orders.dlq", exchange="orders.dlx")
    channel.exchange_declare(exchange="orders", exchange_type="topic", durable=True)
    channel.queue_declare(
        queue="orders.queue", durable=True,
        arguments={"x-dead-letter-exchange": "orders.dlx"},
    )
    channel.queue_bind(queue="orders.queue", exchange="orders", routing_key="orders.created")

    # Orders completed exchange (for shipping service)
    channel.exchange_declare(exchange="orders.completed", exchange_type="topic", durable=True)

    # Notifications exchange (for notification service consumers)
    channel.exchange_declare(exchange="notifications", exchange_type="topic", durable=True)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue="orders.queue", on_message_callback=process_order)

    logger.info("worker_ready", queue="orders.queue")
    channel.start_consuming()


if __name__ == "__main__":
    main()
