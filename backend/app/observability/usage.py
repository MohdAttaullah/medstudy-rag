"""Provider token accounting. Explicitly non-safety-critical.

M11 recorded that cost could not be reported because the provider adapters discarded the `usage`
block from the response. This closes that gap and nothing more: it observes what a call consumed,
and it never participates in whether an answer is released.

Two rules follow from that, and both are enforced by the shape of this module rather than by
convention:

* **A valid answer is never failed for missing usage.** Providers omit the block, change its
  shape, or return it only on some endpoints. `record` accepts anything and extracts what it
  recognises; an unparseable body records nothing and raises nothing.
* **No price is hardcoded.** Rates change and a constant compiled into a report is worse than no
  figure. Token counts are recorded; a rate table is supplied by the deployment if it wants
  currency, and its absence is reported as absence rather than as zero.

Labels are provider and model only. Adding a tenant label would put a per-customer cardinality
explosion into the metrics backend, and adding a correlation id would put one per request.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from prometheus_client import CollectorRegistry, Counter


class UsageSink(Protocol):
    def record(self, provider: str, model_id: str, body: object) -> None: ...


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def extract(body: object) -> TokenUsage | None:
    """Read a usage block from a provider response, tolerating every shape seen in the wild.

    OpenAI reports `prompt_tokens`/`completion_tokens`; Anthropic reports
    `input_tokens`/`output_tokens`. Returns None rather than guessing when neither is present.
    """
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None

    def read(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
        return None

    supplied = read("input_tokens", "prompt_tokens")
    produced = read("output_tokens", "completion_tokens")
    if supplied is None and produced is None:
        return None
    return TokenUsage(supplied or 0, produced or 0)


class ProviderUsageMetrics:
    """Prometheus counters for provider consumption."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.calls = Counter(
            "provider_calls_total",
            "Provider requests that returned a response body",
            ["provider", "model"],
            registry=registry,
        )
        self.input_tokens = Counter(
            "provider_input_tokens_total",
            "Input tokens reported by the provider",
            ["provider", "model"],
            registry=registry,
        )
        self.output_tokens = Counter(
            "provider_output_tokens_total",
            "Output tokens reported by the provider",
            ["provider", "model"],
            registry=registry,
        )
        self.usage_absent = Counter(
            "provider_usage_absent_total",
            "Responses that reported no usage block; recorded rather than assumed to be zero",
            ["provider", "model"],
            registry=registry,
        )

    def record(self, provider: str, model_id: str, body: object) -> None:
        """Never raises. A telemetry failure must not turn a verified answer into an error."""
        try:
            labels = {"provider": provider, "model": model_id}
            self.calls.labels(**labels).inc()
            usage = extract(body)
            if usage is None:
                self.usage_absent.labels(**labels).inc()
                return
            self.input_tokens.labels(**labels).inc(usage.input_tokens)
            self.output_tokens.labels(**labels).inc(usage.output_tokens)
        except Exception:  # noqa: BLE001 - accounting is never worth failing a request over
            return


def estimated_cost(
    usage: TokenUsage, rates: dict[str, dict[str, float]], provider: str, model_id: str
) -> float | None:
    """Cost from a deployment-supplied rate table, or None when no rate is configured.

    Returning None is the point. A missing rate means the deployment has not told this system what
    it pays, and reporting 0.0 would read as "this was free".
    """
    rate = rates.get(f"{provider}:{model_id}")
    if not rate:
        return None
    per_input = rate.get("input_per_1k")
    per_output = rate.get("output_per_1k")
    if per_input is None or per_output is None:
        return None
    return round(usage.input_tokens / 1000 * per_input + usage.output_tokens / 1000 * per_output, 6)


def summarize(body: object, provider: str, model_id: str) -> dict[str, Any]:
    """A sanitized usage record for traces and reports. Never contains prompt or answer text."""
    usage = extract(body)
    return {
        "provider": provider,
        "model_id": model_id,
        "usage_reported": usage is not None,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
    }
