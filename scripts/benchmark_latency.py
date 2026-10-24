"""Latency benchmarking script for HRMS Bot.

Logs in, sends 10 canned questions sequentially over the streaming SSE /chat endpoint,
collects timing events, and prints p50/p95 latency metrics for each stage.
"""

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from app.config import get_settings
from app.db import init_db

CANNED_QUESTIONS = [
    "What is the annual casual leave entitlement?",
    "Show my leave balance",
    "Who is my manager?",
    "What is the maternity leave policy?",
    "Show my current profile details",
    "What are the standard working hours and attendance expectations?",
    "What is the probation period duration?",
    "Show my payslip for month 9 year 2026",
    "Can I carry forward unused leaves to next year?",
    "What is the notice period for resignation?",
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
    return round(sorted_d[f] + d * (sorted_d[c] - sorted_d[f]), 2)


async def run_benchmark(base_url: str = "http://127.0.0.1:8000"):
    print("=" * 70)
    print("HRMS BOT LATENCY BENCHMARK (10 Canned Questions)")
    print("=" * 70)

    # Initialize client: check if server is live, or use in-process ASGI app
    client: Optional[httpx.AsyncClient] = None
    use_asgi = False

    try:
        test_client = httpx.AsyncClient(base_url=base_url, timeout=5.0)
        resp = await test_client.get("/health")
        if resp.status_code == 200:
            client = test_client
            print(f"Connected to running server at {base_url}")
        else:
            await test_client.aclose()
    except Exception:
        pass

    if client is None:
        print("No live server on port 8000. Launching in-process test client with FastAPI app...")
        init_db()
        from app.main import app
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60.0)
        use_asgi = True

    try:
        # Step 1: Login
        print("\nLogging in as Priya Sharma (ACM-005)...")
        login_resp = await client.post(
            "/auth/demo-login",
            json={"emp_code": "ACM-005"},
            headers={"Content-Type": "application/json"},
        )
        if login_resp.status_code != 200:
            # Fallback to direct token if demo-login is disabled
            from app.auth import get_jwt_secret
            from datetime import datetime, timedelta
            token = jwt.encode(
                {"employee_id": 3, "emp_code": "ACM-005", "role": "manager", "token_version": 1, "exp": datetime.utcnow() + timedelta(hours=8)},
                get_jwt_secret(),
                algorithm="HS256",
            )
            client.cookies.set("access_token", token)
            print("Authenticated via generated test JWT cookie.")
        else:
            print("Authenticated successfully via /auth/demo-login.")

        session_id = f"bench_{int(time.time())}"
        stage_records: Dict[str, List[float]] = {
            "auth": [],
            "session_owner_check": [],
            "history_load": [],
            "memory_fetch": [],
            "system_prompt_build": [],
            "policy_search": [],
            "tools_total": [],
            "ttft": [],
            "llm_total": [],
            "db_writes": [],
            "total_request": [],
        }

        print(f"\nRunning {len(CANNED_QUESTIONS)} sequential turns on session '{session_id}':\n")

        for idx, question in enumerate(CANNED_QUESTIONS, 1):
            t_req_start = time.perf_counter()
            timing_data: Optional[Dict[str, Any]] = None
            first_token_time: Optional[float] = None
            token_count = 0

            # Stream chat request
            async with client.stream(
                "POST",
                "/chat",
                json={"session_id": session_id, "message": question},
                headers={"Content-Type": "application/json"},
                timeout=60.0,
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    print(f"[{idx}/10] Query failed (HTTP {response.status_code}): {body.decode()}")
                    continue

                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    blocks = buffer.split("\n\n")
                    buffer = blocks.pop()

                    for block in blocks:
                        if not block.strip():
                            continue
                        event_type = "message"
                        data_val = None
                        for line in block.split("\n"):
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                try:
                                    data_val = json.loads(line[6:])
                                except Exception:
                                    data_val = line[6:]

                        if event_type == "token":
                            if first_token_time is None:
                                first_token_time = (time.perf_counter() - t_req_start) * 1000
                            token_count += 1
                        elif event_type == "timing" and isinstance(data_val, dict):
                            timing_data = data_val

            total_elapsed = round((time.perf_counter() - t_req_start) * 1000, 2)

            if timing_data:
                for k in stage_records.keys():
                    val = float(timing_data.get(k, 0.0))
                    stage_records[k].append(val)
                print(
                    f"[{idx:2d}/10] \"{question[:35]:<35}\" -> Total: {timing_data.get('total_request', total_elapsed):6.1f}ms | "
                    f"TTFT: {timing_data.get('ttft', 0.0):5.1f}ms | LLM: {timing_data.get('llm_total', 0.0):6.1f}ms"
                )
            else:
                stage_records["total_request"].append(total_elapsed)
                if first_token_time:
                    stage_records["ttft"].append(round(first_token_time, 2))
                print(f"[{idx:2d}/10] \"{question[:35]:<35}\" -> Total: {total_elapsed:6.1f}ms")

        # Summary Table
        print("\n" + "=" * 70)
        print("LATENCY BREAKDOWN RESULTS (p50 / p95 in milliseconds)")
        print("=" * 70)
        print(f"{'Stage / Component':<30} | {'p50 (ms)':<12} | {'p95 (ms)':<12} | {'Samples':<8}")
        print("-" * 70)

        stage_labels = [
            ("auth", "Auth / Token Verification"),
            ("session_owner_check", "Session Ownership Check"),
            ("history_load", "History Fetch (Pre-LLM)"),
            ("memory_fetch", "Memories Cache Lookup"),
            ("system_prompt_build", "Prompt Construction"),
            ("policy_search", "Policy Vector Search"),
            ("tools_total", "Tool Executions (Total)"),
            ("ttft", "Time-To-First-Token (TTFT)"),
            ("llm_total", "LLM Stream Generation"),
            ("db_writes", "Database Writes (Bg)"),
            ("total_request", "TOTAL REQUEST TIME"),
        ]

        for key, label in stage_labels:
            vals = stage_records.get(key, [])
            p50 = percentile(vals, 50) if vals else 0.0
            p95 = percentile(vals, 95) if vals else 0.0
            print(f"{label:<30} | {p50:<12.2f} | {p95:<12.2f} | {len(vals):<8}")

        print("=" * 70)

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(run_benchmark())
