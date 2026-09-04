import json
import os
import socket
import time
import logging
import sys

import psycopg2
import redis
import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import StatusCode
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ---------------------------------------------------------------------------
# OpenTelemetry setup
# ---------------------------------------------------------------------------
resource = Resource.create(
    {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "fraud-service"),
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
# Structured logging -- pure JSON to stdout
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

FRAUD_CHECKS_TOTAL = Counter(
    "fraud_checks_total", "Total fraud checks performed", ["decision"],
)
FRAUD_SCORE_DISTRIBUTION = Histogram(
    "fraud_score_distribution", "Distribution of fraud scores",
    buckets=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
)
FRAUD_CHECK_LATENCY = Histogram(
    "fraud_check_latency_seconds", "Latency of fraud check operations",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2],
)
FRAUD_RULES_TRIGGERED = Counter(
    "fraud_rules_triggered_total", "Total fraud rules triggered", ["rule_name"],
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Fraud Analysis Service")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
Instrumentator().instrument(app).expose(app)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/appdb")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    order_id: int
    product: str
    quantity: int
    amount: float

class AnalyzeResponse(BaseModel):
    order_id: int
    score: int
    decision: str
    rules_triggered: list[str]

# ---------------------------------------------------------------------------
# DB / Redis helpers
# ---------------------------------------------------------------------------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def get_redis():
    return redis.from_url(REDIS_URL)

# ---------------------------------------------------------------------------
# Seed rules
# ---------------------------------------------------------------------------
SEED_RULES = [
    ("high_amount", {"max_amount": 5000}, 25),
    ("very_high_amount", {"max_amount": 8000}, 40),
    ("velocity_limit", {"max_per_5min": 10}, 30),
    ("expensive_product_bulk", {"products": ["notebook", "monitor"], "max_quantity": 5}, 20),
    ("suspicious_combo", {"products": ["notebook", "monitor"], "min_total": 3000}, 15),
]

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup():
    conn = get_db()
    cur = conn.cursor()

    # Create tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fraud_rules (
            id SERIAL PRIMARY KEY,
            rule_name VARCHAR(50) NOT NULL UNIQUE,
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
    logger.info("fraud_tables_initialized")

    # Seed fraud rules
    for rule_name, condition, score_weight in SEED_RULES:
        cur.execute(
            """INSERT INTO fraud_rules (rule_name, condition, score_weight)
               VALUES (%s, %s, %s)
               ON CONFLICT (rule_name) DO NOTHING""",
            (rule_name, json.dumps(condition), score_weight),
        )
    conn.commit()
    cur.close()
    conn.close()
    logger.info("fraud_rules_seeded", count=len(SEED_RULES))

# ---------------------------------------------------------------------------
# Fraud check functions (each a child span)
# ---------------------------------------------------------------------------

def check_velocity(product: str) -> tuple[int, int]:
    """Redis INCR+EXPIRE: count orders per product in last 5min.
    Returns (score_addition, velocity_count).
    """
    with tracer.start_as_current_span(
        "check_velocity",
        attributes={"fraud.check": "velocity", "product": product},
    ) as span:
        r = get_redis()
        key = f"fraud:velocity:{product}"
        count = r.incr(key)
        # Set expiry only on first increment (TTL = -1 means no expiry set)
        if r.ttl(key) == -1:
            r.expire(key, 300)  # 5 minutes

        span.set_attribute("fraud.velocity_count", count)
        score = 0
        if count > 10:
            score = 30
            FRAUD_RULES_TRIGGERED.labels(rule_name="velocity_limit").inc()
            logger.info("velocity_limit_triggered", product=product, count=count)

        span.set_attribute("fraud.velocity_score", score)
        return score, count


def check_amount_threshold(amount: float) -> int:
    """If amount > 5000, adds 25. If > 8000, adds 40 (not cumulative)."""
    with tracer.start_as_current_span(
        "check_amount_threshold",
        attributes={"fraud.check": "amount_threshold", "fraud.amount": amount},
    ) as span:
        score = 0
        if amount > 8000:
            score = 40
            FRAUD_RULES_TRIGGERED.labels(rule_name="very_high_amount").inc()
            logger.info("very_high_amount_triggered", amount=amount)
        elif amount > 5000:
            score = 25
            FRAUD_RULES_TRIGGERED.labels(rule_name="high_amount").inc()
            logger.info("high_amount_triggered", amount=amount)

        span.set_attribute("fraud.amount_score", score)
        return score


def check_product_rules(product: str, quantity: int, amount: float) -> tuple[int, list[str]]:
    """Postgres lookup in fraud_rules table. Matches rule conditions against order."""
    with tracer.start_as_current_span(
        "check_product_rules",
        attributes={
            "fraud.check": "product_rules",
            "product": product,
            "quantity": quantity,
            "fraud.amount": amount,
        },
    ) as span:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT rule_name, condition, score_weight FROM fraud_rules WHERE enabled = true"
        )
        rules = cur.fetchall()
        cur.close()
        conn.close()

        score = 0
        triggered = []

        for rule_name, condition, score_weight in rules:
            cond = condition if isinstance(condition, dict) else json.loads(condition)
            matched = False

            # expensive_product_bulk: product in list AND quantity > max_quantity
            if "products" in cond and "max_quantity" in cond:
                if product in cond["products"] and quantity > cond["max_quantity"]:
                    matched = True

            # suspicious_combo: product in list AND amount > min_total
            elif "products" in cond and "min_total" in cond:
                if product in cond["products"] and amount > cond["min_total"]:
                    matched = True

            if matched:
                score += score_weight
                triggered.append(rule_name)
                FRAUD_RULES_TRIGGERED.labels(rule_name=rule_name).inc()
                logger.info("product_rule_triggered", rule=rule_name, product=product)

        span.set_attribute("fraud.product_rules_score", score)
        span.set_attribute("fraud.product_rules_triggered", len(triggered))
        return score, triggered


def calculate_score(
    velocity_score: int,
    amount_score: int,
    product_score: int,
    triggered_rules: list[str],
) -> tuple[int, str]:
    """Combines all checks, makes decision."""
    with tracer.start_as_current_span(
        "calculate_score",
        attributes={
            "fraud.velocity_score": velocity_score,
            "fraud.amount_score": amount_score,
            "fraud.product_score": product_score,
        },
    ) as span:
        total_score = velocity_score + amount_score + product_score
        # Cap at 100
        total_score = min(total_score, 100)

        if total_score < 40:
            decision = "approved"
        elif total_score <= 80:
            decision = "review"
        else:
            decision = "rejected"

        span.set_attribute("fraud.total_score", total_score)
        span.set_attribute("fraud.decision", decision)
        span.set_attribute("fraud.rules_count", len(triggered_rules))

        logger.info(
            "fraud_score_calculated",
            score=total_score,
            decision=decision,
            rules_triggered=triggered_rules,
        )
        return total_score, decision

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/analyze", response_model=AnalyzeResponse)
def analyze_order(req: AnalyzeRequest):
    start_time = time.monotonic()

    with tracer.start_as_current_span(
        "fraud_analyze",
        attributes={
            "order.id": req.order_id,
            "product": req.product,
            "quantity": req.quantity,
            "fraud.amount": req.amount,
        },
    ) as span:
        # 1. Velocity check
        velocity_score, velocity_count = check_velocity(req.product)

        # 2. Amount threshold check
        amount_score = check_amount_threshold(req.amount)

        # 3. Product rules check
        product_score, product_triggered = check_product_rules(
            req.product, req.quantity, req.amount
        )

        # 4. Combine and decide
        all_triggered = []
        if velocity_score > 0:
            all_triggered.append("velocity_limit")
        if amount_score == 25:
            all_triggered.append("high_amount")
        elif amount_score == 40:
            all_triggered.append("very_high_amount")
        all_triggered.extend(product_triggered)

        total_score, decision = calculate_score(
            velocity_score, amount_score, product_score, all_triggered
        )

        # Set span attributes
        span.set_attribute("fraud.score", total_score)
        span.set_attribute("fraud.decision", decision)
        span.set_attribute("fraud.rules_evaluated", len(all_triggered))
        span.set_attribute("fraud.velocity_count", velocity_count)

        # Span events
        if decision == "review":
            span.add_event(
                "fraud.review_required",
                attributes={
                    "fraud.score": total_score,
                    "fraud.rules_triggered": json.dumps(all_triggered),
                },
            )
        elif decision == "rejected":
            span.add_event(
                "fraud.rejected",
                attributes={
                    "fraud.score": total_score,
                    "fraud.rules_triggered": json.dumps(all_triggered),
                },
            )

        # Persist fraud check result
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO fraud_checks (order_id, score, decision, rules_triggered)
               VALUES (%s, %s, %s, %s)""",
            (req.order_id, total_score, decision, all_triggered),
        )
        conn.commit()
        cur.close()
        conn.close()

        # Update Prometheus metrics
        latency = time.monotonic() - start_time
        FRAUD_CHECKS_TOTAL.labels(decision=decision).inc(exemplar=_trace_exemplar())
        FRAUD_SCORE_DISTRIBUTION.observe(total_score)
        FRAUD_CHECK_LATENCY.observe(latency)

        logger.info(
            "fraud_analysis_complete",
            order_id=req.order_id,
            score=total_score,
            decision=decision,
            rules_triggered=all_triggered,
            latency=round(latency, 4),
        )

        return AnalyzeResponse(
            order_id=req.order_id,
            score=total_score,
            decision=decision,
            rules_triggered=all_triggered,
        )


@app.get("/health")
def health():
    return {"status": "healthy", "service": "fraud-service"}


@app.get("/checks/{order_id}")
def get_fraud_check(order_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, order_id, score, decision, rules_triggered, checked_at
           FROM fraud_checks WHERE order_id = %s ORDER BY id DESC LIMIT 1""",
        (order_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Fraud check not found for this order")

    return {
        "check_id": row[0],
        "order_id": row[1],
        "score": row[2],
        "decision": row[3],
        "rules_triggered": row[4] or [],
        "checked_at": str(row[5]),
    }
