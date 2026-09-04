import os
import random
import socket
import time
import uuid

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
from opentelemetry.trace import StatusCode
from prometheus_client import Counter, Histogram, Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "payment-gateway"),
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
def _trace_exemplar():
    """Return current trace_id as exemplar for metric->trace correlation."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return {}


PAYMENT_REQUESTS = Counter(
    "payment_gateway_requests_total",
    "Total payment requests",
    ["method", "status"],
)
PAYMENT_LATENCY = Histogram(
    "payment_gateway_latency_seconds",
    "Payment processing latency",
    ["method"],
    buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0],
)
PAYMENT_DECLINE_RATE = Gauge(
    "payment_decline_rate",
    "Recent payment decline rate",
)

# Track totals for decline rate calculation
_total_requests = 0
_declined_requests = 0

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Payment Gateway")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
Instrumentator().instrument(app).expose(app)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/appdb")

# Simulated latency per payment method (seconds)
METHOD_LATENCY = {
    "pix": 0.1,
    "debit_card": 0.3,
    "credit_card": 0.5,
}

# Decline error codes
DECLINE_REASONS = [
    "insufficient_funds",
    "card_expired",
    "fraud_suspected",
    "issuer_unavailable",
    "invalid_card_number",
]

DECLINE_RATE = 0.05  # 5% random decline rate


def get_db():
    return psycopg2.connect(DATABASE_URL)


class PaymentRequest(BaseModel):
    order_id: int
    amount: float
    method: str  # credit_card, debit_card, pix


@app.on_event("startup")
def startup():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id SERIAL PRIMARY KEY,
            order_id INT NOT NULL,
            method VARCHAR(20) NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            status VARCHAR(20) NOT NULL,
            decline_reason VARCHAR(100),
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("payment_gateway_initialized", table="payment_transactions")


# ---------------------------------------------------------------------------
# Internal processing steps (child spans)
# ---------------------------------------------------------------------------

def validate_card(method: str, amount: float) -> bool:
    """Validate payment method details. Returns True if valid."""
    with tracer.start_as_current_span(
        "validate_card",
        attributes={"payment.method": method, "payment.amount": amount},
    ) as span:
        # Simulate validation time
        time.sleep(random.uniform(0.01, 0.03))

        # All methods are "valid" in simulation — real validation would check card numbers
        valid = True
        span.set_attribute("validation.result", valid)
        logger.info("card_validated", method=method, valid=valid)
        return valid


def fraud_check(order_id: int, amount: float, method: str) -> dict:
    """Simple internal fraud check. Returns risk assessment."""
    with tracer.start_as_current_span(
        "fraud_check",
        attributes={
            "order_id": order_id,
            "payment.amount": amount,
            "payment.method": method,
        },
    ) as span:
        # Simulate fraud analysis time
        time.sleep(random.uniform(0.02, 0.05))

        # Simple risk scoring: higher amounts = higher risk
        risk_score = min(amount / 10000.0, 1.0)
        risk_level = "low" if risk_score < 0.3 else "medium" if risk_score < 0.7 else "high"

        span.set_attribute("fraud.risk_score", risk_score)
        span.set_attribute("fraud.risk_level", risk_level)

        logger.info(
            "fraud_check_completed",
            order_id=order_id,
            risk_score=round(risk_score, 3),
            risk_level=risk_level,
        )
        return {"risk_score": risk_score, "risk_level": risk_level}


def process_payment(order_id: int, amount: float, method: str) -> dict:
    """Process the payment with simulated latency and random declines."""
    global _total_requests, _declined_requests

    with tracer.start_as_current_span(
        "process_payment",
        attributes={
            "order_id": order_id,
            "payment.method": method,
            "payment.amount": amount,
        },
    ) as span:
        # Simulate method-specific latency
        base_latency = METHOD_LATENCY.get(method, 0.3)
        latency = base_latency + random.uniform(-0.05, 0.1)
        time.sleep(max(latency, 0.05))

        span.set_attribute("payment.latency_seconds", latency)

        # Random decline
        _total_requests += 1
        declined = random.random() < DECLINE_RATE

        if declined:
            _declined_requests += 1
            decline_reason = random.choice(DECLINE_REASONS)
            span.set_attribute("payment.status", "declined")
            span.set_attribute("payment.decline_reason", decline_reason)
            span.set_status(StatusCode.ERROR, f"Payment declined: {decline_reason}")
            logger.warning(
                "payment_declined",
                order_id=order_id,
                method=method,
                reason=decline_reason,
            )
            result = {"status": "declined", "decline_reason": decline_reason}
        else:
            span.set_attribute("payment.status", "approved")
            logger.info("payment_approved", order_id=order_id, method=method, amount=amount)
            result = {"status": "approved", "decline_reason": None}

        # Update decline rate gauge
        if _total_requests > 0:
            PAYMENT_DECLINE_RATE.set(round(_declined_requests / _total_requests, 4))

        return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "healthy", "service": "payment-gateway"}


@app.post("/process")
def process_payment_endpoint(req: PaymentRequest):
    if req.method not in METHOD_LATENCY:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payment method: {req.method}. Must be one of: {list(METHOD_LATENCY.keys())}",
        )

    with tracer.start_as_current_span(
        "payment_gateway.process",
        attributes={
            "order_id": req.order_id,
            "payment.method": req.method,
            "payment.amount": req.amount,
        },
    ) as span:
        start = time.monotonic()

        # Step 1: Validate card/method
        validate_card(req.method, req.amount)

        # Step 2: Fraud check
        fraud_result = fraud_check(req.order_id, req.amount, req.method)
        span.set_attribute("fraud.risk_level", fraud_result["risk_level"])

        # Step 3: Process payment
        payment_result = process_payment(req.order_id, req.amount, req.method)
        status = payment_result["status"]
        decline_reason = payment_result["decline_reason"]

        # Record transaction in DB
        transaction_id = str(uuid.uuid4())
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO payment_transactions (order_id, method, amount, status, decline_reason)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (req.order_id, req.method, req.amount, status, decline_reason),
        )
        db_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        elapsed = time.monotonic() - start

        # Record metrics
        PAYMENT_REQUESTS.labels(method=req.method, status=status).inc(
            exemplar=_trace_exemplar()
        )
        PAYMENT_LATENCY.labels(method=req.method).observe(
            elapsed, exemplar=_trace_exemplar()
        )

        span.set_attribute("payment.transaction_id", db_id)
        span.set_attribute("payment.status", status)
        span.set_attribute("payment.duration_seconds", elapsed)

        logger.info(
            "payment_processed",
            order_id=req.order_id,
            transaction_id=db_id,
            method=req.method,
            amount=req.amount,
            status=status,
            duration=round(elapsed, 3),
        )

        response = {
            "transaction_id": db_id,
            "order_id": req.order_id,
            "status": status,
            "method": req.method,
            "amount": req.amount,
        }
        if decline_reason:
            response["decline_reason"] = decline_reason

        return response


@app.get("/transactions/{order_id}")
def get_transaction(order_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, order_id, method, amount, status, decline_reason, created_at
           FROM payment_transactions
           WHERE order_id = %s
           ORDER BY id DESC LIMIT 1""",
        (order_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found for this order")

    return {
        "transaction_id": row[0],
        "order_id": row[1],
        "method": row[2],
        "amount": float(row[3]),
        "status": row[4],
        "decline_reason": row[5],
        "created_at": str(row[6]),
    }
