"""Parse metrics.

Label cardinality stays bounded: only fixed error codes and severities are ever used as label
values. Document, version, tenant and parse-run identifiers are correlation-log fields, never
metric labels.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram


class ParseMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.started = Counter("parse_jobs_total", "Parse attempts started", registry=registry)
        self.succeeded = Counter(
            "parse_jobs_succeeded_total",
            "Parse attempts reaching READY_FOR_CHUNKING",
            registry=registry,
        )
        self.failed = Counter(
            "parse_jobs_failed_total", "Failed parse attempts", ["code"], registry=registry
        )
        self.needs_review = Counter(
            "parse_jobs_needs_review_total", "Parse attempts routed to review", registry=registry
        )
        self.cancelled = Counter(
            "parse_jobs_cancelled_total",
            "Parse attempts released after cancellation",
            registry=registry,
        )
        self.duration = Histogram(
            "parse_duration_seconds",
            "End-to-end parse pipeline duration",
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
            registry=registry,
        )
        self.pages = Counter("parse_pages_total", "Pages persisted", registry=registry)
        self.tables = Counter("parse_tables_total", "Tables extracted", registry=registry)
        self.figures = Counter("parse_figures_total", "Figures extracted", registry=registry)
        self.formulas = Counter("parse_formulas_total", "Formulas extracted", registry=registry)
        self.ocr_pages = Counter(
            "parse_ocr_pages_total", "Pages recorded as OCR-derived", registry=registry
        )
        self.findings = Counter(
            "parse_validation_findings_total",
            "Persisted parse-quality findings",
            ["severity"],
            registry=registry,
        )
