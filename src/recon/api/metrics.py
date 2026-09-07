"""Prometheus instruments for `GET /metrics`. `docs/contracts.md` section 5.

Deliberately minimal — request counts, `/investigate` latency, and in-flight
concurrency. Anything richer (spans, correlation on request ID) is step 12's
tracing, not this step.
"""

from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "recon_api_requests_total",
    "Requests handled, by route and status code.",
    ["route", "status"],
)

INVESTIGATE_LATENCY = Histogram(
    "recon_api_investigate_duration_seconds",
    "POST /investigate latency, including the underlying agent run.",
)

INVESTIGATE_IN_FLIGHT = Gauge(
    "recon_api_investigate_in_flight",
    "POST /investigate requests currently running.",
)
