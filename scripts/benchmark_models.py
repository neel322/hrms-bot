import asyncio
import os
import sys
import time
from typing import Dict, List, Optional
import statistics

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agent import reset_model_cache, run_agent
from app.auth import resolve_caller
from app.config import get_settings
from app.db import init_db

BENCHMARK_QUERIES = [
    {"query": "How many casual leaves do I have left?", "caller_code": "ACM-008", "label": "Casual leaves balance"},
    {"query": "What is the WFH policy?", "caller_code": "ACM-008", "label": "WFH policy search"},
    {"query": "Who is on leave this week?", "caller_code": "ACM-005", "label": "Team on leave (manager)"},
    {"query": "Hi", "caller_code": "ACM-008", "label": "Greeting"},
    {"query": "When is the next company holiday?", "caller_code": "ACM-008", "label": "Upcoming holiday"},
    {"query": "Can I see my last payslip?", "caller_code": "ACM-008", "label": "Last payslip"},
    {"query": "What was my attendance last month?", "caller_code": "ACM-008", "label": "Attendance last month"},
    {"query": "Who is my manager and what is my designation?", "caller_code": "ACM-008", "label": "Profile & manager"},
]


def percentile(data: List[float], p: float) -> float:
    """Computes the p-th percentile (0 <= p <= 100) of a list of floats."""
    if not data:
        return 0.0
    sorted_d = sorted(data)
    k = (len(sorted_d) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_d) - 1)
    d = k - f
    return sorted_d[f] + d * (sorted_d[c] - sorted_d[f])


async def benchmark_single_query(query: str, caller_code: str, model_name: str) -> Dict[str, float]:
    """Runs a single query through run_agent and measures TTFT and total duration."""
    caller = resolve_caller(caller_code)
    session_id = f"bench_{int(time.time() * 1000)}_{caller_code}"

    # Override active model for this run
    settings = get_settings()
    original_model = settings.groq_model
    settings.groq_model = model_name
    reset_model_cache()

    ttft: Optional[float] = None
    t0 = time.perf_counter()
    tokens = []

    try:
        async for event in run_agent(caller=caller, session_id=session_id, user_message=query):
            if event["event"] == "token":
                if ttft is None:
                    ttft = time.perf_counter() - t0
                tokens.append(event["data"]["token"])
            elif event["event"] == "error":
                print(f"    [WARN] Event error on query '{query}': {event.get('data')}")
    except Exception as exc:
        print(f"    [ERROR] Exception on query '{query}': {exc}")

    t1 = time.perf_counter()
    total_time = t1 - t0
    if ttft is None:
        ttft = total_time

    # Restore
    settings.groq_model = original_model
    reset_model_cache()

    return {
        "ttft": ttft,
        "total_time": total_time,
        "tokens_count": len(tokens),
    }


async def run_benchmark():
    init_db()
    settings = get_settings()
    primary_model = settings.groq_model
    fallback_model = settings.groq_fallback_model

    print("=" * 80)
    print(f"HRMS BOT MODEL LATENCY BENCHMARK")
    print(f"Primary Model:  {primary_model}")
    print(f"Fallback Model: {fallback_model}")
    print("=" * 80)

    results_primary: List[Dict[str, float]] = []
    results_fallback: List[Dict[str, float]] = []

    print(f"\n--- Running 8 Queries on Primary Model ({primary_model}) ---")
    for idx, q_info in enumerate(BENCHMARK_QUERIES, 1):
        q = q_info["query"]
        caller_code = q_info["caller_code"]
        print(f"[{idx}/8] Running: \"{q}\" ({caller_code})...", end="", flush=True)
        res = await benchmark_single_query(q, caller_code, primary_model)
        results_primary.append(res)
        print(f" TTFT: {res['ttft']:.3f}s | Total: {res['total_time']:.3f}s")
        await asyncio.sleep(0.5)

    print(f"\n--- Running 8 Queries on Fallback Model ({fallback_model}) ---")
    for idx, q_info in enumerate(BENCHMARK_QUERIES, 1):
        q = q_info["query"]
        caller_code = q_info["caller_code"]
        print(f"[{idx}/8] Running: \"{q}\" ({caller_code})...", end="", flush=True)
        res = await benchmark_single_query(q, caller_code, fallback_model)
        results_fallback.append(res)
        print(f" TTFT: {res['ttft']:.3f}s | Total: {res['total_time']:.3f}s")
        await asyncio.sleep(0.5)

    # Comparison Table
    print("\n" + "=" * 105)
    print(f"{'Query':<36} | {'Primary TTFT':<14} | {'Primary Total':<14} | {'Fallback TTFT':<14} | {'Fallback Total':<14}")
    print("-" * 105)

    for q_info, p_res, f_res in zip(BENCHMARK_QUERIES, results_primary, results_fallback):
        q_label = q_info["query"]
        if len(q_label) > 34:
            q_label = q_label[:31] + "..."
        print(f"{q_label:<36} | {p_res['ttft']:>11.3f} s | {p_res['total_time']:>11.3f} s | {f_res['ttft']:>12.3f} s | {f_res['total_time']:>12.3f} s")

    print("-" * 105)

    p_ttfts = [r["ttft"] for r in results_primary]
    p_totals = [r["total_time"] for r in results_primary]
    f_ttfts = [r["ttft"] for r in results_fallback]
    f_totals = [r["total_time"] for r in results_fallback]

    print(f"{'Mean':<36} | {statistics.mean(p_ttfts):>11.3f} s | {statistics.mean(p_totals):>11.3f} s | {statistics.mean(f_ttfts):>12.3f} s | {statistics.mean(f_totals):>12.3f} s")
    print(f"{'p50 (Median)':<36} | {percentile(p_ttfts, 50):>11.3f} s | {percentile(p_totals, 50):>11.3f} s | {percentile(f_ttfts, 50):>12.3f} s | {percentile(f_totals, 50):>12.3f} s")
    print(f"{'p95':<36} | {percentile(p_ttfts, 95):>11.3f} s | {percentile(p_totals, 95):>11.3f} s | {percentile(f_ttfts, 95):>12.3f} s | {percentile(f_totals, 95):>12.3f} s")
    print("=" * 105)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
