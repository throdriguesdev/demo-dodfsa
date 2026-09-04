import json
import os
import socket
import time
import uuid

import redis
import structlog
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "inventory-service"),
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
# Custom Prometheus metrics
# ---------------------------------------------------------------------------
RESERVATION_TTL = int(os.getenv("RESERVATION_TTL", "60"))


def _trace_exemplar():
    """Return current trace_id as exemplar for metric->trace correlation."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return {}


INVENTORY_OPERATIONS = Counter(
    "inventory_operations_total",
    "Total inventory operations",
    ["operation", "product"],
)
STOCK_LEVEL = Gauge(
    "stock_level",
    "Current stock level per product",
    ["product"],
)
RESERVATION_DURATION = Histogram(
    "reservation_duration_seconds",
    "Duration of inventory reservations",
    buckets=[1, 5, 10, 30, 60, 120, 300],
)
INVENTORY_ERRORS = Counter(
    "inventory_errors_total",
    "Total inventory errors",
    ["operation", "reason"],
)

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ReserveRequest(BaseModel):
    order_id: int
    product: str
    quantity: int


class ConfirmRequest(BaseModel):
    order_id: int


class RollbackRequest(BaseModel):
    order_id: int
    product: str
    quantity: int


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="LGTM Demo Inventory Service")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
Instrumentator().instrument(app).expose(app)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

PRODUCTS = [
    "notebook", "keyboard", "monitor", "mouse",
    "headset", "webcam", "cable", "hub",
]


def get_redis():
    return redis.from_url(REDIS_URL)


@app.on_event("startup")
def startup():
    """Sync stock levels into Prometheus gauges on startup."""
    r = get_redis()
    for product in PRODUCTS:
        stock = r.hget(f"product:{product}", "stock")
        if stock is not None:
            STOCK_LEVEL.labels(product=product).set(int(stock))
    logger.info("inventory_service_started", products=len(PRODUCTS))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "healthy", "service": "inventory-service"}


@app.get("/stock")
def get_all_stock():
    """Return stock levels for all products."""
    with tracer.start_as_current_span("get_all_stock") as span:
        r = get_redis()
        result = {}
        for product in PRODUCTS:
            data = r.hgetall(f"product:{product}")
            if data:
                stock = int(data.get(b"stock", data.get("stock", 0)))
                price = float(data.get(b"price", data.get("price", 0)))
                STOCK_LEVEL.labels(product=product).set(stock)
                result[product] = {
                    "stock": stock,
                    "price": price,
                    "status": "ok" if stock > 10 else "low" if stock > 0 else "out_of_stock",
                }
            else:
                result[product] = {"stock": 0, "price": 0, "status": "unknown"}
        span.set_attribute("products.count", len(result))
        return result


@app.get("/stock/{product}")
def get_product_stock(product: str):
    """Return stock level for a specific product."""
    with tracer.start_as_current_span(
        "get_product_stock", attributes={"product": product}
    ) as span:
        r = get_redis()
        data = r.hgetall(f"product:{product}")
        if not data:
            INVENTORY_ERRORS.labels(operation="get_stock", reason="product_not_found").inc()
            raise HTTPException(status_code=404, detail=f"Product '{product}' not found")

        stock = int(data.get(b"stock", data.get("stock", 0)))
        price = float(data.get(b"price", data.get("price", 0)))
        STOCK_LEVEL.labels(product=product).set(stock)
        span.set_attribute("stock.level", stock)
        return {
            "product": product,
            "stock": stock,
            "price": price,
            "status": "ok" if stock > 10 else "low" if stock > 0 else "out_of_stock",
        }


@app.post("/reserve")
def reserve_stock(req: ReserveRequest):
    """Reserve stock for an order. Decrements stock and creates a reservation with TTL."""
    with tracer.start_as_current_span(
        "reserve_stock",
        attributes={
            "order.id": req.order_id,
            "product": req.product,
            "quantity": req.quantity,
        },
    ) as span:
        r = get_redis()

        # If reservation already exists for this order, clean it up first (idempotent)
        existing = r.exists(f"reservation:{req.order_id}")
        if existing:
            logger.info(
                "replacing_existing_reservation",
                order_id=req.order_id,
                product=req.product,
            )
            r.delete(f"reservation:{req.order_id}")

        # Check available stock
        with tracer.start_as_current_span(
            "check_available_stock",
            attributes={"product": req.product},
        ) as check_span:
            stock_raw = r.hget(f"product:{req.product}", "stock")
            if stock_raw is None:
                INVENTORY_ERRORS.labels(operation="reserve", reason="product_not_found").inc()
                span.set_status(StatusCode.ERROR, "Product not found")
                raise HTTPException(
                    status_code=404,
                    detail=f"Product '{req.product}' not found",
                )
            available = int(stock_raw)
            check_span.set_attribute("stock.available", available)

        # Auto-replenish: restock when low (keeps demo running)
        if available < 10:
            with tracer.start_as_current_span(
                "auto_replenish",
                attributes={"product": req.product, "old_stock": available},
            ):
                r.hset(f"product:{req.product}", "stock", 200)
                available = 200
                STOCK_LEVEL.labels(product=req.product).set(200)
                logger.info(
                    "auto_replenished",
                    product=req.product,
                    new_stock=200,
                )

        if available < req.quantity:
            INVENTORY_ERRORS.labels(operation="reserve", reason="insufficient_stock").inc()
            span.set_status(StatusCode.ERROR, "Insufficient stock")
            logger.warning(
                "insufficient_stock",
                order_id=req.order_id,
                product=req.product,
                available=available,
                requested=req.quantity,
            )
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for '{req.product}': {available} available, {req.quantity} requested",
            )

        # Decrement stock
        with tracer.start_as_current_span(
            "decrement_stock",
            attributes={"product": req.product, "quantity": req.quantity},
        ):
            new_stock = r.hincrby(f"product:{req.product}", "stock", -req.quantity)
            STOCK_LEVEL.labels(product=req.product).set(new_stock)

        # Create reservation with TTL
        reservation_id = str(uuid.uuid4())
        with tracer.start_as_current_span(
            "create_reservation",
            attributes={
                "reservation.id": reservation_id,
                "reservation.ttl": RESERVATION_TTL,
            },
        ):
            reservation_data = {
                "reservation_id": reservation_id,
                "order_id": req.order_id,
                "product": req.product,
                "quantity": req.quantity,
                "created_at": time.time(),
            }
            r.hset(
                f"reservation:{req.order_id}",
                mapping={k: str(v) for k, v in reservation_data.items()},
            )
            r.expire(f"reservation:{req.order_id}", RESERVATION_TTL)

        INVENTORY_OPERATIONS.labels(operation="reserve", product=req.product).inc()
        span.set_attribute("reservation.id", reservation_id)
        span.set_attribute("stock.remaining", new_stock)

        logger.info(
            "stock_reserved",
            order_id=req.order_id,
            product=req.product,
            quantity=req.quantity,
            reservation_id=reservation_id,
            remaining_stock=new_stock,
        )

        return {
            "reservation_id": reservation_id,
            "order_id": req.order_id,
            "product": req.product,
            "quantity": req.quantity,
            "status": "reserved",
            "ttl_seconds": RESERVATION_TTL,
        }


@app.post("/confirm")
def confirm_reservation(req: ConfirmRequest):
    """Confirm a reservation (remove TTL, delete reservation key)."""
    with tracer.start_as_current_span(
        "confirm_reservation",
        attributes={"order.id": req.order_id},
    ) as span:
        r = get_redis()

        # Fetch reservation
        with tracer.start_as_current_span("fetch_reservation"):
            res_data = r.hgetall(f"reservation:{req.order_id}")

        if not res_data:
            INVENTORY_ERRORS.labels(operation="confirm", reason="reservation_not_found").inc()
            span.set_status(StatusCode.ERROR, "Reservation not found")
            logger.warning("reservation_not_found", order_id=req.order_id)
            raise HTTPException(
                status_code=404,
                detail=f"Reservation not found for order {req.order_id}",
            )

        product = (res_data.get(b"product") or res_data.get("product", b"")).decode() if isinstance(
            res_data.get(b"product", res_data.get("product", "")), bytes
        ) else str(res_data.get("product", ""))
        quantity = int(res_data.get(b"quantity", res_data.get("quantity", 0)))
        created_at = float(res_data.get(b"created_at", res_data.get("created_at", 0)))

        # Calculate reservation duration for histogram
        duration = time.time() - created_at if created_at else 0
        RESERVATION_DURATION.observe(duration)

        # Remove reservation key (stock already decremented during reserve)
        with tracer.start_as_current_span("delete_reservation"):
            r.delete(f"reservation:{req.order_id}")

        INVENTORY_OPERATIONS.labels(operation="confirm", product=product).inc()
        span.set_attribute("reservation.duration_seconds", round(duration, 2))
        span.set_attribute("product", product)
        span.set_attribute("quantity", quantity)

        logger.info(
            "reservation_confirmed",
            order_id=req.order_id,
            product=product,
            quantity=quantity,
            duration_seconds=round(duration, 2),
        )

        return {
            "order_id": req.order_id,
            "product": product,
            "quantity": quantity,
            "status": "confirmed",
            "reservation_duration_seconds": round(duration, 2),
        }


@app.post("/rollback")
def rollback_stock(req: RollbackRequest):
    """Rollback a reservation: restore stock in Redis."""
    with tracer.start_as_current_span(
        "rollback_stock",
        attributes={
            "order.id": req.order_id,
            "product": req.product,
            "quantity": req.quantity,
        },
    ) as span:
        r = get_redis()

        # Check product exists
        with tracer.start_as_current_span(
            "verify_product",
            attributes={"product": req.product},
        ):
            exists = r.exists(f"product:{req.product}")
            if not exists:
                INVENTORY_ERRORS.labels(operation="rollback", reason="product_not_found").inc()
                span.set_status(StatusCode.ERROR, "Product not found")
                raise HTTPException(
                    status_code=404,
                    detail=f"Product '{req.product}' not found",
                )

        # Restore stock
        with tracer.start_as_current_span(
            "restore_stock",
            attributes={"product": req.product, "quantity": req.quantity},
        ):
            new_stock = r.hincrby(f"product:{req.product}", "stock", req.quantity)
            STOCK_LEVEL.labels(product=req.product).set(new_stock)

        # Clean up reservation if it still exists
        with tracer.start_as_current_span("cleanup_reservation"):
            res_data = r.hgetall(f"reservation:{req.order_id}")
            if res_data:
                created_at = float(res_data.get(b"created_at", res_data.get("created_at", 0)))
                duration = time.time() - created_at if created_at else 0
                RESERVATION_DURATION.observe(duration)
                r.delete(f"reservation:{req.order_id}")

        INVENTORY_OPERATIONS.labels(operation="rollback", product=req.product).inc()
        span.set_attribute("stock.restored_to", new_stock)

        logger.info(
            "stock_rolled_back",
            order_id=req.order_id,
            product=req.product,
            quantity=req.quantity,
            new_stock=new_stock,
        )

        return {
            "order_id": req.order_id,
            "product": req.product,
            "quantity": req.quantity,
            "status": "rolled_back",
            "current_stock": new_stock,
        }
