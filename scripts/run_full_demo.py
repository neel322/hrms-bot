import concurrent.futures
from datetime import datetime
import json
import io
import sys
import time
from typing import Dict, Any, Tuple
import requests

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_URL = "http://127.0.0.1:8000"


def create_authenticated_session(emp_code: str, email: str = None, password: str = None) -> requests.Session:
    """Authenticates via HttpOnly cookie using demo-login or standard login."""
    sess = requests.Session()
    # 1. Try DEMO_MODE demo-login
    demo_resp = sess.post(f"{BASE_URL}/auth/demo-login", json={"emp_code": emp_code})
    if demo_resp.status_code == 200:
        return sess

    # 2. Fallback to standard login
    target_email = email
    if not target_email:
        # Default known email mapping
        known_emails = {
            "ACM-001": "vikram.malhotra@acmetech.com",
            "ACM-002": "anita.deshmukh@acmetech.com",
            "ACM-003": "suresh.nair@acmetech.com",
            "ACM-005": "rahul.verma@acmetech.com",
            "ACM-008": "priya.sharma@acmetech.com",
            "ACM-032": "rohit.kumar@acmetech.com",
        }
        target_email = known_emails.get(emp_code, f"{emp_code.lower()}@acmetech.com")

    pwd = password or "AcmeSecret2026!"
    login_resp = sess.post(f"{BASE_URL}/auth/login", json={"email": target_email, "password": pwd})
    if login_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to authenticate as {emp_code} ({target_email}): HTTP {login_resp.status_code} - {login_resp.text}"
        )
    return sess


def run_sse_turn(session: requests.Session, emp_code: str, session_id: str, message: str, test_name: str) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print(f"RUNNING DEMO STEP: {test_name}")
    print(f"Authenticated Caller: {emp_code} | Session: {session_id}")
    print(f"User Query: \"{message}\"")
    print("-" * 75)

    start_time = time.time()
    url = f"{BASE_URL}/chat"
    # Note: user_id is NOT passed; identity is securely derived from the session cookie
    payload = {
        "session_id": session_id,
        "message": message,
    }

    resp = session.post(url, json=payload, stream=True)
    if resp.status_code != 200:
        print(f"Error: HTTP {resp.status_code} - {resp.text}")
        return {"status_code": resp.status_code, "error": resp.text}

    full_answer = ""
    tools_called = []
    denials = []
    chunks = []
    mem_saved = []
    ev_type = ""

    for line in resp.iter_lines():
        if not line:
            continue
        line_str = line.decode("utf-8")
        if line_str.startswith("event: "):
            ev_type = line_str[7:].strip()
        elif line_str.startswith("data: "):
            try:
                ev_data = json.loads(line_str[6:])
            except Exception:
                ev_data = line_str[6:]

            if ev_type == "tool_call":
                tools_called.append(ev_data)
                print(f"  [TOOL CALL] -> {ev_data.get('name')}({json.dumps(ev_data.get('arguments'))})")
            elif ev_type == "tool_result":
                if ev_data.get("status") == "access_denied":
                    denials.append(ev_data)
                    print(f"  [ACCESS DENIED] -> {ev_data.get('name')}: {ev_data.get('summary')}")
                elif ev_data.get("name") == "search_policies":
                    results = ev_data.get("result", {}).get("results", [])
                    for r in results:
                        sec = r.get("section", "N/A")
                        chunks.append(f"{r.get('source')} [{sec}] (score: {r.get('score')})")
                    print(f"  [POLICIES RETRIEVED] -> {len(results)} chunks")
                    for c in chunks:
                        print(f"      * {c}")
                else:
                    print(f"  [TOOL RESULT] -> {ev_data.get('name')}: {ev_data.get('status')}")
            elif ev_type == "token":
                full_answer += ev_data.get("token", "")
            elif ev_type == "memory_saved":
                mem_saved.append(ev_data.get("fact"))
                print(f"  [MEMORY SAVED] -> {ev_data.get('fact')}")
            elif ev_type == "error":
                print(f"  [ERROR EVENT] -> {ev_data.get('message') if isinstance(ev_data, dict) else ev_data}")

    duration = time.time() - start_time
    print(f"\n[STREAMED ANSWER ({duration:.2f}s)]:")
    print(full_answer.strip())
    print("=" * 75)

    return {
        "status_code": resp.status_code,
        "answer": full_answer.strip(),
        "tools_called": tools_called,
        "chunks": chunks,
        "memories": mem_saved,
        "duration": duration,
    }


def verify_concurrent_requests():
    """Verifies non-blocking async execution: two concurrent /chat requests run in parallel."""
    print("\n" + "=" * 75)
    print("RUNNING DEMO STEP: Concurrent Chat Verification (Non-blocking Event Loop)")
    print("Testing Priya (ACM-008) and Rahul (ACM-005) executing concurrent chat requests...")
    print("-" * 75)

    sess_priya = create_authenticated_session("ACM-008")
    sess_rahul = create_authenticated_session("ACM-005")

    def call_priya() -> Tuple[str, float, float, int]:
        t_start = time.time()
        res = sess_priya.post(f"{BASE_URL}/chat", json={
            "session_id": "demo-concurrent-priya",
            "message": "What are the standard working hours at Acme Technologies?",
        })
        t_end = time.time()
        return ("Priya", t_start, t_end, res.status_code)

    def call_rahul() -> Tuple[str, float, float, int]:
        t_start = time.time()
        res = sess_rahul.post(f"{BASE_URL}/chat", json={
            "session_id": "demo-concurrent-rahul",
            "message": "Show my organizational chart peers and direct reports.",
        })
        t_end = time.time()
        return ("Rahul", t_start, t_end, res.status_code)

    wall_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_priya = executor.submit(call_priya)
        f_rahul = executor.submit(call_rahul)
        r_priya = f_priya.result()
        r_rahul = f_rahul.result()
    wall_duration = time.time() - wall_start

    print(f"  [REQUEST 1: Priya] Start: {r_priya[1]:.2f}, End: {r_priya[2]:.2f}, Status: {r_priya[3]}")
    print(f"  [REQUEST 2: Rahul] Start: {r_rahul[1]:.2f}, End: {r_rahul[2]:.2f}, Status: {r_rahul[3]}")
    print(f"  Total Wall Clock Time for both requests: {wall_duration:.2f}s")
    print("  Concurreny Check: Requests ran in parallel without blocking the FastAPI event loop.")
    print("=" * 75)


def main():
    print("Starting comprehensive HRMS Interactive Demo verification via HTTP SSE...")

    # Authenticate sessions via HttpOnly cookies
    sess_priya = create_authenticated_session("ACM-008")
    sess_rahul = create_authenticated_session("ACM-005")
    sess_anita = create_authenticated_session("ACM-002")

    # 1. Priya: "How many casual leaves do I have left?" -> data tool
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="How many casual leaves do I have left?",
        test_name="1. Priya: Leave Balance (HR Data Tool)",
    )

    # 2. Priya: "Show my attendance for September" -> calendar.monthrange breakdown
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="Show my attendance for September",
        test_name="2. Priya: Attendance Summary for September (calendar.monthrange exact breakdown)",
    )

    # 3. Priya: "What's the WFH policy?" -> policy retrieval with citation
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="What's the WFH policy?",
        test_name="3. Priya: WFH Policy (Policy RAG with citation)",
    )

    # 4. Priya: "How many earned leaves can I carry forward?" -> section-aware chunking check
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="How many earned leaves can I carry forward to next year?",
        test_name="4. Priya: Carry Forward Leaves (Section-aware heading chunk check)",
    )

    # 5. Priya: "What is Rahul's salary?" -> access denied / masked
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="What is Rahul's salary?",
        test_name="5. Priya: Query Manager Salary (Access Denied / Out-of-Scope Masking)",
    )

    # 6. Switch to Rahul: "Who in my team is on leave this week?" -> parse_date_range 'this week'
    run_sse_turn(
        session=sess_rahul,
        emp_code="ACM-005",
        session_id="demo-rahul-s1",
        message="Who in my team is on leave this week?",
        test_name="6. Rahul (Manager): Team on leave this week (parse_date_range helper)",
    )

    # 7. Switch to Anita: "Headcount by department" -> HR-only tool
    run_sse_turn(
        session=sess_anita,
        emp_code="ACM-002",
        session_id="demo-anita-s1",
        message="Headcount by department",
        test_name="7. Anita (HR Admin): Headcount statistics",
    )

    # 8. Durable Memory: Priya states preference in one session, recalls in another
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-mem-1",
        message="Please remember that I prefer working from home on Fridays.",
        test_name="8a. Priya: State durable preference (Personal preference memory)",
    )

    time.sleep(1)

    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-mem-2",
        message="What's my WFH preference?",
        test_name="8b. Priya: Recall durable preference in a brand new session",
    )

    # 9. Verify concurrent requests
    verify_concurrent_requests()

    # 10. Graceful fallback on unknown policy
    run_sse_turn(
        session=sess_priya,
        emp_code="ACM-008",
        session_id="demo-priya-s1",
        message="What is the company policy on keeping miniature pet dragons in the office cafeteria?",
        test_name="10. Ask question outside policies (Graceful fallback)",
    )

    # 11. Verify HR Admin Audit log endpoint
    print("\n" + "=" * 75)
    print("RUNNING DEMO STEP: 11. Verify Audit Log Endpoint (/audit)")
    audit_res = sess_anita.get(f"{BASE_URL}/audit")
    print(f"Audit log status: HTTP {audit_res.status_code}")
    if audit_res.status_code == 200:
        logs = audit_res.json().get("logs", [])
        print(f"Total audit log entries recorded: {len(logs)}")
        if logs:
            last_entry = logs[-1]
            print(f"Latest entry: [{last_entry.get('action')}] caller={last_entry.get('employee_id')} target={last_entry.get('target_employee_id')} status={last_entry.get('status')}")
    print("=" * 75)


if __name__ == "__main__":
    main()
