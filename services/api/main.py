import contextvars
import json
import os
import random
import socket
import time

import pika
import psycopg2
import redis
import structlog
from fastapi import FastAPI, HTTPException
from opentelemetry import baggage, propagate, trace
from opentelemetry.context import attach, detach
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, SpanContext, SpanKind, TraceFlags
from prometheus_client import Counter, Histogram, Gauge, Info
from prometheus_fastapi_instrumentator import Instrumentator

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "demo-api"),
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

# ---------------------------------------------------------------------------
# Structured logging — pure JSON to stdout
# ---------------------------------------------------------------------------
import logging
import sys

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
# Custom Prometheus metrics (business metrics with exemplars)
# ---------------------------------------------------------------------------
def _trace_exemplar():
    """Return current trace_id as exemplar for metric→trace correlation."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return {}

ORDERS_CREATED = Counter(
    "orders_created_total", "Total orders created", ["product"],
)
ORDERS_REVENUE = Counter(
    "orders_revenue_total", "Total revenue from completed orders", ["product"],
)
ORDERS_COMPLETED = Counter(
    "orders_completed_total", "Total orders completed", ["product"],
)
ORDERS_FAILED = Counter(
    "orders_failed_total", "Total orders failed", ["product", "reason"],
)
ORDERS_CANCELLED = Counter(
    "orders_cancelled_total", "Total orders cancelled",
)
ORDER_PROCESSING_DURATION = Histogram(
    "order_processing_duration_seconds", "Time to process an order end-to-end",
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10],
)
INVENTORY_LEVEL = Gauge(
    "inventory_level", "Current stock level per product", ["product"],
)
PAYMENTS_TOTAL = Counter(
    "payments_total", "Total payments processed", ["method", "status"],
)
NOTIFICATIONS_SENT = Counter(
    "notifications_sent_total", "Total notifications sent", ["channel", "status"],
)
ORDERS_IN_QUEUE = Gauge(
    "orders_in_queue", "Approximate orders waiting in queue",
)
CACHE_HITS = Counter("cache_hits_total", "Cache hits", ["entity"])
CACHE_MISSES = Counter("cache_misses_total", "Cache misses", ["entity"])

APP_INFO = Info("app", "Application info")
APP_INFO.info({"version": "1.0.0", "service": "demo-api", "stack": "lgtm"})

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="LGTM Demo API")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
Instrumentator().instrument(app).expose(app)

@app.get("/")
def root():
    return {
        "service": "demo-api",
        "stack": "LGTM (Loki, Grafana, Tempo, Mimir) + Alloy",
        "docs": "/docs",
        "metrics": "/metrics",
        "health": "/health",
    }

import urllib.request
import urllib.parse
import urllib.error

MIMIR_URL = os.getenv("MIMIR_URL", "http://mimir:9009")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")


def _proxy_get(url, timeout=5):
    """Simple proxy helper for LGTM backend APIs."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/metrics/{job}")
def get_service_metrics(job: str):
    """Fetch available metric names for a service job from Mimir."""
    url = f"{MIMIR_URL}/prometheus/api/v1/label/__name__/values"
    all_metrics = _proxy_get(url)
    names = all_metrics.get("data", [])
    # Also fetch current values for a few key metrics
    results = []
    for name in names[:100]:
        q = urllib.parse.quote(f'{name}{{job="{job}"}}')
        try:
            data = _proxy_get(f"{MIMIR_URL}/prometheus/api/v1/query?query={q}", timeout=2)
            if data.get("data", {}).get("result"):
                for r in data["data"]["result"]:
                    results.append({
                        "name": name,
                        "labels": {k: v for k, v in r["metric"].items() if k != "__name__"},
                        "value": r["value"][1] if r.get("value") else None,
                    })
        except Exception:
            pass
    return {"job": job, "metrics": results}


@app.get("/api/traces/recent")
def get_recent_traces():
    """Fetch recent traces from Tempo."""
    url = f"{TEMPO_URL}/api/search?limit=5&q=status%3Dok"
    try:
        data = _proxy_get(url, timeout=5)
    except Exception:
        url = f"{TEMPO_URL}/api/search?limit=5"
        data = _proxy_get(url, timeout=5)
    traces = data.get("traces", [])
    return {"traces": traces[:5]}


@app.get("/api/logs/recent")
def get_recent_logs():
    """Fetch recent structured logs from Loki."""
    query = urllib.parse.quote('{container="api"} | json | event != ""')
    url = f"{LOKI_URL}/loki/api/v1/query_range?query={query}&limit=10&direction=backward"
    data = _proxy_get(url, timeout=5)
    result = data.get("data", {}).get("result", [])
    entries = []
    for stream in result[:3]:
        for ts, line in stream.get("values", [])[:5]:
            try:
                parsed = json.loads(line)
                entries.append(parsed)
            except Exception:
                entries.append({"raw": line})
    return {"logs": entries[:10]}


@app.get("/api/metrics/sample")
def get_sample_metrics():
    """Fetch sample metrics showing different types from Mimir."""
    samples = {}
    queries = {
        "counter": 'orders_created_total',
        "gauge": 'orders_in_queue',
        "histogram": 'order_processing_duration_seconds_bucket{le="1"}',
    }
    for mtype, query in queries.items():
        q = urllib.parse.quote(query)
        try:
            data = _proxy_get(f"{MIMIR_URL}/prometheus/api/v1/query?query={q}", timeout=2)
            results = data.get("data", {}).get("result", [])
            if results:
                samples[mtype] = {
                    "query": query,
                    "result": [{"metric": r["metric"], "value": r["value"][1]} for r in results[:3]],
                }
        except Exception:
            pass
    return {"samples": samples}


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/appdb")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

PRODUCTS = {
    "notebook": 1299.99, "keyboard": 149.99, "monitor": 499.99,
    "mouse": 79.99, "headset": 199.99, "webcam": 129.99,
    "cable": 29.99, "hub": 89.99,
}

# Context var for passing bulk span info into individual create_order calls
_bulk_context_var: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("bulk_context", default=None)

def get_db():
    return psycopg2.connect(DATABASE_URL)

def get_redis():
    return redis.from_url(REDIS_URL)

def get_rabbitmq():
    params = pika.URLParameters(RABBITMQ_URL)
    params.heartbeat = 600
    params.blocked_connection_timeout = 300
    return pika.BlockingConnection(params)


@app.on_event("startup")
def startup():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            product VARCHAR(100) NOT NULL,
            quantity INT NOT NULL,
            unit_price NUMERIC(10,2),
            total NUMERIC(10,2),
            status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        );
        DO $$ BEGIN
            ALTER TABLE orders ADD COLUMN shipping_status VARCHAR(20) DEFAULT NULL;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
        DO $$ BEGIN
            ALTER TABLE orders ADD COLUMN tracking_code VARCHAR(50) DEFAULT NULL;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
        DO $$ BEGIN
            ALTER TABLE orders ADD COLUMN customer_name VARCHAR(100) DEFAULT NULL;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            order_id INT REFERENCES orders(id),
            method VARCHAR(20) NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            order_id INT REFERENCES orders(id),
            channel VARCHAR(20) NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS order_events (
            id SERIAL PRIMARY KEY,
            order_id INT REFERENCES orders(id),
            event VARCHAR(50) NOT NULL,
            service VARCHAR(50) NOT NULL,
            data JSONB,
            trace_id VARCHAR(32),
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id);
        CREATE INDEX IF NOT EXISTS idx_order_events_trace_id ON order_events(trace_id);
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id SERIAL PRIMARY KEY,
            order_id INT NOT NULL,
            method VARCHAR(20) NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            status VARCHAR(20) NOT NULL,
            decline_reason VARCHAR(100),
            created_at TIMESTAMP DEFAULT NOW()
        );
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
        CREATE TABLE IF NOT EXISTS fraud_rules (
            id SERIAL PRIMARY KEY,
            rule_name VARCHAR(50) NOT NULL,
            condition JSONB NOT NULL,
            score_weight INT NOT NULL DEFAULT 20,
            enabled BOOLEAN DEFAULT true
        );
        CREATE TABLE IF NOT EXISTS fraud_checks (
            id SERIAL PRIMARY KEY,
            order_id INT NOT NULL,
            score INT NOT NULL,
            decision VARCHAR(20) NOT NULL,
            rules_triggered TEXT[],
            checked_at TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("database_initialized")

    r = get_redis()
    for product, price in PRODUCTS.items():
        r.hset(f"product:{product}", mapping={"price": price, "stock": 100})
        INVENTORY_LEVEL.labels(product=product).set(100)
    logger.info("product_catalog_seeded", count=len(PRODUCTS))

    for attempt in range(10):
        try:
            rmq = get_rabbitmq()
            ch = rmq.channel()
            ch.exchange_declare(exchange="orders.dlx", exchange_type="fanout", durable=True)
            ch.queue_declare(queue="orders.dlq", durable=True)
            ch.queue_bind(queue="orders.dlq", exchange="orders.dlx")
            ch.exchange_declare(exchange="orders", exchange_type="topic", durable=True)
            ch.queue_declare(
                queue="orders.queue", durable=True,
                arguments={"x-dead-letter-exchange": "orders.dlx"},
            )
            ch.queue_bind(queue="orders.queue", exchange="orders", routing_key="orders.created")
            rmq.close()
            logger.info("rabbitmq_topology_initialized")
            break
        except Exception:
            logger.warning("rabbitmq_init_retry", attempt=attempt + 1)
            time.sleep(3)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/products")
def list_products():
    r = get_redis()
    catalog = {}
    for product in PRODUCTS:
        data = r.hgetall(f"product:{product}")
        stock = int(data[b"stock"])
        catalog[product] = {"price": float(data[b"price"]), "stock": stock}
        INVENTORY_LEVEL.labels(product=product).set(stock)
    return catalog


@app.get("/inventory")
def get_inventory():
    """Inventory levels with low-stock warnings."""
    r = get_redis()
    inventory = {}
    for product in PRODUCTS:
        stock = int(r.hget(f"product:{product}", "stock") or 0)
        INVENTORY_LEVEL.labels(product=product).set(stock)
        inventory[product] = {
            "stock": stock,
            "status": "ok" if stock > 10 else "low" if stock > 0 else "out_of_stock",
        }
    return inventory


@app.get("/orders")
def list_orders():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, product, quantity, total, status, created_at FROM orders ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"orders": [
        {"id": r[0], "product": r[1], "quantity": r[2], "total": float(r[3]) if r[3] else None,
         "status": r[4], "created_at": str(r[5])}
        for r in rows
    ]}


@app.post("/orders")
def create_order(customer_name: str = None, product: str = None, quantity: int = None):
    product = product if product and product in PRODUCTS else random.choice(list(PRODUCTS.keys()))
    quantity = quantity if quantity and 1 <= quantity <= 10 else random.randint(1, 10)
    unit_price = PRODUCTS[product]

    # --- 2.3 Baggage: propagate business context across services ---
    priority = random.choice(["normal", "normal", "normal", "high", "rush"])
    tier = random.choice(["free", "free", "premium"])
    ctx = baggage.set_baggage("order_priority", priority)
    ctx = baggage.set_baggage("customer_tier", tier, context=ctx)
    ctx = baggage.set_baggage("customer_name", customer_name or "anonymous", context=ctx)
    token = attach(ctx)

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (product, quantity, unit_price, customer_name) VALUES (%s, %s, %s, %s) RETURNING id",
            (product, quantity, unit_price, customer_name),
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # Store trace context for span links (used by cancel_order)
        current_span_ctx = trace.get_current_span().get_span_context()
        creation_trace_id = format(current_span_ctx.trace_id, "032x") if current_span_ctx.trace_id else None
        creation_span_id = format(current_span_ctx.span_id, "016x") if current_span_ctx.trace_id else None

        r = get_redis()
        r.setex(f"order:{order_id}", 300, json.dumps({
            "id": order_id, "product": product, "quantity": quantity,
            "unit_price": unit_price, "status": "pending",
            "customer_name": customer_name,
            "trace_id": creation_trace_id, "span_id": creation_span_id,
        }))

        # --- 2.2 Span Links: include bulk context if called from /orders/bulk ---
        msg = {"order_id": order_id, "product": product, "quantity": quantity, "customer_name": customer_name}
        bulk_ctx = _bulk_context_var.get(None)
        if bulk_ctx:
            msg["bulk_trace_id"] = bulk_ctx[0]
            msg["bulk_span_id"] = bulk_ctx[1]
        body_json = json.dumps(msg)

        with tracer.start_as_current_span(
            "orders publish", kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.destination.name": "orders",
                "messaging.rabbitmq.routing_key": "orders.created",
                "messaging.message.conversation_id": str(order_id),
                "messaging.message.body.size": len(body_json),
                "order.priority": priority,
                "customer.tier": tier,
                "customer.name": customer_name or "anonymous",
            },
        ) as span:
            # --- 2.1 Span Event: order_created ---
            span.add_event("order_created", {
                "order_id": order_id, "product": product,
                "quantity": quantity, "unit_price": unit_price,
                "priority": priority, "customer_tier": tier,
                "customer_name": customer_name or "anonymous",
            })
            headers = {}
            propagate.inject(carrier=headers)
            rmq = get_rabbitmq()
            ch = rmq.channel()
            ch.basic_publish(
                exchange="orders", routing_key="orders.created",
                body=body_json,
                properties=pika.BasicProperties(
                    content_type="application/json", delivery_mode=2, headers=headers,
                ),
            )
            rmq.close()

        ORDERS_CREATED.labels(product=product).inc(exemplar=_trace_exemplar())
        current_trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        logger.info("order_created", order_id=order_id, product=product, quantity=quantity,
                     priority=priority, customer_tier=tier, customer_name=customer_name)
        return {
            "id": order_id, "product": product, "quantity": quantity,
            "status": "pending", "customer_name": customer_name,
            "trace_id": current_trace_id,
        }
    finally:
        detach(token)


@app.post("/orders/bulk")
def create_bulk_orders(count: int = 5):
    """Create multiple orders — each worker trace links back to this bulk span."""
    with tracer.start_as_current_span(
        "bulk_create_orders", attributes={"bulk.count": min(count, 20)},
    ) as bulk_span:
        bulk_span_ctx = bulk_span.get_span_context()
        _bulk_context_var.set(
            (format(bulk_span_ctx.trace_id, "032x"), format(bulk_span_ctx.span_id, "016x"))
        )
        try:
            created = []
            for _ in range(min(count, 20)):
                result = create_order()
                created.append(result)
            bulk_span.add_event("bulk_orders_created", {"count": len(created)})
        finally:
            _bulk_context_var.set(None)
        return {"created": len(created), "orders": created}


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    r = get_redis()
    cached = r.get(f"order:{order_id}")
    if cached:
        CACHE_HITS.labels(entity="order").inc()
        logger.info("order_cache_hit", order_id=order_id)
        order = json.loads(cached.decode())
        order["source"] = "cache"
        return order

    CACHE_MISSES.labels(entity="order").inc()
    logger.info("order_cache_miss", order_id=order_id)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, product, quantity, unit_price, total, status FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "id": row[0], "product": row[1], "quantity": row[2],
        "unit_price": float(row[3]) if row[3] else None,
        "total": float(row[4]) if row[4] else None,
        "status": row[5], "source": "database",
    }


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int):
    """Cancel a pending order — creates a span link to the original creation trace."""
    # --- 2.2 Span Links: look up original trace for linking ---
    r = get_redis()
    links = []
    cached = r.get(f"order:{order_id}")
    if cached:
        order_data = json.loads(cached.decode())
        orig_tid = order_data.get("trace_id")
        orig_sid = order_data.get("span_id")
        if orig_tid and orig_sid:
            orig_span_ctx = SpanContext(
                trace_id=int(orig_tid, 16), span_id=int(orig_sid, 16),
                is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            links.append(Link(orig_span_ctx, {"link.type": "original_order"}))

    with tracer.start_as_current_span("cancel_order", links=links, attributes={"order_id": order_id}):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT product, quantity, status FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="Order not found")

        product, quantity, status = row
        if status not in ("pending", "processing"):
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail=f"Cannot cancel order in '{status}' status")

        cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))
        conn.commit()
        cur.close()
        conn.close()

        r.hincrby(f"product:{product}", "stock", quantity)
        r.delete(f"order:{order_id}")

        ORDERS_CANCELLED.inc(exemplar=_trace_exemplar())
        logger.info("order_cancelled", order_id=order_id, product=product, quantity=quantity)
        return {"id": order_id, "status": "cancelled"}


@app.get("/orders/{order_id}/payment")
def get_payment(order_id: int):
    """Get payment details for an order (checks both payments and payment_transactions tables)."""
    conn = get_db()
    cur = conn.cursor()
    # Check legacy payments table first
    cur.execute(
        "SELECT id, method, amount, status, created_at FROM payments WHERE order_id = %s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    row = cur.fetchone()
    if row:
        cur.close(); conn.close()
        return {
            "payment_id": row[0], "order_id": order_id, "method": row[1],
            "amount": float(row[2]), "status": row[3], "created_at": str(row[4]),
        }
    # Check payment_transactions table (used by payment-gateway service)
    cur.execute(
        "SELECT id, method, amount, status, decline_reason, created_at FROM payment_transactions WHERE order_id = %s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {
        "payment_id": row[0], "order_id": order_id, "method": row[1],
        "amount": float(row[2]), "status": row[3],
        "decline_reason": row[4], "created_at": str(row[5]),
    }


@app.get("/stats")
def stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'completed') as completed,
            COUNT(*) FILTER (WHERE status = 'failed') as failed,
            COUNT(*) FILTER (WHERE status = 'pending') as pending,
            COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
            COALESCE(SUM(total) FILTER (WHERE status = 'completed'), 0) as revenue
        FROM orders
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {
        "total_orders": row[0], "completed": row[1], "failed": row[2],
        "pending": row[3], "cancelled": row[4], "revenue": float(row[5]),
    }


@app.get("/stats/products")
def product_stats():
    """Revenue and order count per product."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT product, COUNT(*) as orders, COALESCE(SUM(total), 0) as revenue,
               AVG(total) as avg_total
        FROM orders WHERE status = 'completed'
        GROUP BY product ORDER BY revenue DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"products": [
        {"product": r[0], "orders": r[1], "revenue": float(r[2]), "avg_order": float(r[3]) if r[3] else 0}
        for r in rows
    ]}


@app.get("/stats/payments")
def payment_stats():
    """Payment breakdown by method."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT method, COUNT(*) as count, SUM(amount) as total
        FROM payments WHERE status = 'approved'
        GROUP BY method ORDER BY total DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"methods": [
        {"method": r[0], "count": r[1], "total": float(r[2])}
        for r in rows
    ]}


@app.post("/webhooks/notify")
def notify_webhook(order_id: int, status: str, channel: str = "email"):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications (order_id, channel, message) VALUES (%s, %s, %s) RETURNING id",
        (order_id, channel, f"Order {order_id} is now {status}"),
    )
    notif_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    NOTIFICATIONS_SENT.labels(channel=channel, status=status).inc()
    logger.info("notification_created", notification_id=notif_id, order_id=order_id, status=status)
    return {"notification_id": notif_id}


# Called by the worker to update business metrics
@app.post("/webhooks/order-completed")
def order_completed_webhook(order_id: int, product: str, total: float, method: str, duration: float = 0):
    ORDERS_COMPLETED.labels(product=product).inc(exemplar=_trace_exemplar())
    ORDERS_REVENUE.labels(product=product).inc(total, exemplar=_trace_exemplar())
    PAYMENTS_TOTAL.labels(method=method, status="approved").inc(exemplar=_trace_exemplar())
    if duration > 0:
        ORDER_PROCESSING_DURATION.observe(duration, exemplar=_trace_exemplar())
    return {"ok": True}


@app.post("/webhooks/order-failed")
def order_failed_webhook(order_id: int, product: str, reason: str):
    ORDERS_FAILED.labels(product=product, reason=reason).inc(exemplar=_trace_exemplar())
    return {"ok": True}


@app.get("/slow")
def slow_endpoint():
    delay = random.uniform(0.5, 2.0)
    logger.warning("slow_request_started", delay_seconds=round(delay, 2))
    time.sleep(delay)
    return {"message": "done", "delay": round(delay, 2)}


@app.get("/error")
def error_endpoint():
    logger.error("intentional_error", reason="demo error simulation")
    raise HTTPException(status_code=500, detail="Simulated error for demo")


@app.get("/stats/detailed")
def detailed_stats():
    """JOIN across orders + payments + shipments + fraud_checks."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.id, o.product, o.status, o.total,
               COALESCE(p.method, pt.method) as payment_method,
               COALESCE(p.amount, pt.amount) as payment_amount,
               s.carrier, s.shipping_cost, s.tracking_code,
               f.score as fraud_score, f.decision as fraud_decision
        FROM orders o
        LEFT JOIN payments p ON p.order_id = o.id
        LEFT JOIN payment_transactions pt ON pt.order_id = o.id AND pt.status = 'approved'
        LEFT JOIN shipments s ON s.order_id = o.id
        LEFT JOIN fraud_checks f ON f.order_id = o.id
        WHERE o.created_at > NOW() - interval '1 hour'
        ORDER BY o.created_at DESC LIMIT 100
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"orders": [
        {
            "id": r[0], "product": r[1], "status": r[2],
            "total": float(r[3]) if r[3] else None,
            "payment_method": r[4], "payment_amount": float(r[5]) if r[5] else None,
            "carrier": r[6], "shipping_cost": float(r[7]) if r[7] else None,
            "tracking_code": r[8],
            "fraud_score": r[9], "fraud_decision": r[10],
        }
        for r in rows
    ]}


@app.get("/stats/funnel")
def funnel_stats():
    """CTE-based order conversion funnel."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        WITH funnel AS (
            SELECT
                COUNT(*) as total_orders,
                COUNT(*) FILTER (WHERE status NOT IN ('rejected')) as passed_fraud,
                COUNT(*) FILTER (WHERE status IN ('completed', 'shipped')) as paid,
                COUNT(*) FILTER (WHERE shipping_status = 'dispatched') as shipped
            FROM orders
            WHERE created_at > NOW() - interval '1 hour'
        )
        SELECT *,
            ROUND(passed_fraud::numeric / NULLIF(total_orders, 0) * 100, 1) as fraud_pass_rate,
            ROUND(paid::numeric / NULLIF(passed_fraud, 0) * 100, 1) as payment_success_rate,
            ROUND(shipped::numeric / NULLIF(paid, 0) * 100, 1) as shipping_rate
        FROM funnel
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {"total_orders": 0, "passed_fraud": 0, "paid": 0, "shipped": 0}
    return {
        "total_orders": row[0], "passed_fraud": row[1], "paid": row[2], "shipped": row[3],
        "fraud_pass_rate": float(row[4]) if row[4] else 0,
        "payment_success_rate": float(row[5]) if row[5] else 0,
        "shipping_rate": float(row[6]) if row[6] else 0,
    }


@app.get("/orders/{order_id}/full")
def get_order_full(order_id: int):
    """Full order lifecycle — order + payment + shipment + fraud check + events."""
    conn = get_db()
    cur = conn.cursor()

    # Order
    cur.execute(
        "SELECT id, product, quantity, unit_price, total, status, shipping_status, tracking_code, created_at FROM orders WHERE id = %s",
        (order_id,),
    )
    order_row = cur.fetchone()
    if not order_row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Order not found")

    result = {
        "id": order_row[0], "product": order_row[1], "quantity": order_row[2],
        "unit_price": float(order_row[3]) if order_row[3] else None,
        "total": float(order_row[4]) if order_row[4] else None,
        "status": order_row[5],
        "shipping_status": order_row[6], "tracking_code": order_row[7],
        "created_at": str(order_row[8]),
    }

    # Payment (check both legacy and new payment tables)
    cur.execute(
        """SELECT id, method, amount, status, created_at FROM payment_transactions WHERE order_id = %s
           UNION ALL
           SELECT id, method, amount, status, created_at FROM payments WHERE order_id = %s
           ORDER BY created_at DESC LIMIT 1""",
        (order_id, order_id),
    )
    pay_row = cur.fetchone()
    if pay_row:
        result["payment"] = {
            "payment_id": pay_row[0], "method": pay_row[1], "amount": float(pay_row[2]),
            "status": pay_row[3], "created_at": str(pay_row[4]),
        }

    # Shipment
    cur.execute(
        "SELECT id, carrier, tracking_code, shipping_cost, estimated_days, status, created_at FROM shipments WHERE order_id = %s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    ship_row = cur.fetchone()
    if ship_row:
        result["shipping"] = {
            "shipment_id": ship_row[0], "carrier": ship_row[1], "tracking_code": ship_row[2],
            "shipping_cost": float(ship_row[3]) if ship_row[3] else None,
            "estimated_days": ship_row[4], "status": ship_row[5], "created_at": str(ship_row[6]),
        }

    # Fraud check
    cur.execute(
        "SELECT id, score, decision, rules_triggered, checked_at FROM fraud_checks WHERE order_id = %s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    fraud_row = cur.fetchone()
    if fraud_row:
        result["fraud"] = {
            "check_id": fraud_row[0], "score": fraud_row[1], "decision": fraud_row[2],
            "rules_triggered": fraud_row[3], "checked_at": str(fraud_row[4]),
        }

    # Order events timeline
    cur.execute(
        "SELECT event, service, data, trace_id, created_at FROM order_events WHERE order_id = %s ORDER BY created_at ASC",
        (order_id,),
    )
    events = cur.fetchall()
    result["events"] = [
        {"event": e[0], "service": e[1], "data": e[2], "trace_id": e[3], "created_at": str(e[4])}
        for e in events
    ]

    cur.close()
    conn.close()
    return result
