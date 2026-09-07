"""Reproducible load test against the running stack. Deterministic providers by default.

    uv run python scripts/load_test.py --ask 60 --concurrency 6
    uv run python scripts/load_test.py --reads 200 --concurrency 12

What this measures is how the *system* behaves under concurrency: whether it stays bounded, keeps
refusing safely, and degrades predictably. It is not a benchmark of the models, and the numbers it
prints are development-host measurements, never production SLOs.

Ask requests are exercised against whatever the deployment has configured. On a stack with no
generator configured — the default — every request abstains at the gate without a provider call,
which is exactly the load profile worth testing repeatedly because it is free and deterministic.
`--live-provider` opts into spending money, and is never implied.

The pass criteria are about *predictable failure*, not throughput: no 5xx, no connection error, no
unbounded latency growth, and every response still obeying the verified-answer rule.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings  # noqa: E402

QUESTIONS = (
    "What does the synthetic reference say about the illustrative dose?",
    "Which invented enzyme is described in the evaluation corpus?",
    "What interval does the fictional guideline give?",
    "Describe a marker that does not exist in this corpus.",
)


async def _one(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        body: dict[str, Any] = {}
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                body = response.json()
            except ValueError:
                body = {}
        return {
            "status": response.status_code,
            "ms": elapsed,
            "outcome": body.get("outcome"),
            "verified": body.get("verified"),
            "answered": body.get("answer") is not None,
        }
    except httpx.HTTPError as exc:
        return {
            "status": 0,
            "ms": (time.perf_counter() - started) * 1000,
            "error": type(exc).__name__,
        }


async def _drive(
    base_url: str,
    headers: dict[str, str],
    plan: list[tuple[str, str, dict[str, Any]]],
    workers: int,
) -> list[dict[str, Any]]:
    """Bounded concurrency. An unbounded gather would measure the client, not the server."""
    results: list[dict[str, Any]] = []
    queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()
    for item in plan:
        queue.put_nowait(item)

    limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=120, limits=limits
    ) as client:

        async def worker() -> None:
            while True:
                try:
                    method, url, kwargs = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                results.append(await _one(client, method, url, **kwargs))
                queue.task_done()

        await asyncio.gather(*(worker() for _ in range(workers)))
    return results


def summarize(label: str, results: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    latencies = sorted(r["ms"] for r in results)
    statuses: dict[str, int] = {}
    for result in results:
        statuses[str(result["status"])] = statuses.get(str(result["status"]), 0) + 1

    def percentile(fraction: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * fraction))], 1)

    return {
        "scenario": label,
        "requests": len(results),
        "seconds": round(elapsed, 2),
        "throughput_rps": round(len(results) / elapsed, 2) if elapsed else None,
        "status_counts": statuses,
        "transport_errors": sum(1 for r in results if r["status"] == 0),
        "server_errors": sum(1 for r in results if 500 <= int(r["status"]) < 600),
        "rate_limited": statuses.get("429", 0),
        "latency_ms": {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": round(latencies[-1], 1) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        },
        "outcomes": {
            outcome: sum(1 for r in results if r.get("outcome") == outcome)
            for outcome in {r.get("outcome") for r in results if r.get("outcome")}
        },
        # The safety property under load: an answer only ever alongside a VERIFIED outcome.
        "answers_without_verified": sum(
            1 for r in results if r.get("answered") and r.get("outcome") != "VERIFIED"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--ask", type=int, default=40, help="Concurrent Ask submissions.")
    parser.add_argument("--reads", type=int, default=120, help="Concurrent authorized reads.")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--json", type=Path, default=ROOT / ".local/m12-load.json")
    parser.add_argument(
        "--live-provider",
        action="store_true",
        help="Permit provider-backed Ask load. Costs money; never implied.",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=str(ROOT / ".env"))
    if settings.generator is not None and not args.live_provider:
        print(
            "A generator is configured, so Ask load would spend real money.\n"
            "Re-run with --live-provider to accept that, or unset the generator for a\n"
            "deterministic run. Read-only load will still be measured."
        )
        args.ask = 0

    reader = next((c for c in settings.dev_principals if c.role == "reader"), None)
    admin = next((c for c in settings.dev_principals if c.role == "admin"), None)
    if reader is None or admin is None:
        raise SystemExit("Development identities are required to drive load.")

    report: dict[str, Any] = {
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "provider_backed": bool(settings.generator and args.live_provider),
        "environment_note": (
            "Development-host measurement under Docker Desktop on Windows. Not a production SLO."
        ),
        "scenarios": [],
    }

    if args.reads:
        plan = [("GET", "/api/v1/documents", {}) for _ in range(args.reads)]
        started = time.perf_counter()
        results = asyncio.run(
            _drive(
                args.base_url,
                {"Authorization": "Bearer " + admin.token.get_secret_value()},
                plan,
                args.concurrency,
            )
        )
        report["scenarios"].append(
            summarize("authorized_reads", results, time.perf_counter() - started)
        )

    if args.ask:
        plan = [
            ("POST", "/api/v1/ask", {"json": {"question": QUESTIONS[i % len(QUESTIONS)]}})
            for i in range(args.ask)
        ]
        started = time.perf_counter()
        results = asyncio.run(
            _drive(
                args.base_url,
                {"Authorization": "Bearer " + reader.token.get_secret_value()},
                plan,
                args.concurrency,
            )
        )
        report["scenarios"].append(summarize("ask", results, time.perf_counter() - started))

    for scenario in report["scenarios"]:
        print(json.dumps(scenario, indent=2))

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWritten to {args.json.relative_to(ROOT)}")

    # Predictable failure is the criterion, not throughput.
    problems = (
        [
            f"{s['scenario']}: {s['server_errors']} server errors"
            for s in report["scenarios"]
            if s["server_errors"]
        ]
        + [
            f"{s['scenario']}: {s['transport_errors']} transport errors"
            for s in report["scenarios"]
            if s["transport_errors"]
        ]
        + [
            f"{s['scenario']}: {s['answers_without_verified']} answers without a verified outcome"
            for s in report["scenarios"]
            if s["answers_without_verified"]
        ]
    )
    if problems:
        print("\nFAIL: " + "; ".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print("\nPASS: no server errors, no transport errors, verified-answer rule held under load.")


if __name__ == "__main__":
    main()
