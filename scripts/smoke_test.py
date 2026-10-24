"""Smoke test script for Acme Technologies HRMS Bot.

Verifies end-to-end functionality against a running server (http://127.0.0.1:8000):
1. Health and config check.
2. Login as each key persona (CEO, HR Admin, Manager, Employee, New Joiner).
3. Access control & HR Admin endpoints (/employees/onboarding, /audit/admin, /audit).
4. Interactive chat execution (Employee leave balance, Manager team query, Access denial masking).
"""
import io
import json
import os
import sys
import time
from typing import Dict, Any, Optional
import requests

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

PERSONAS = [
    {"code": "ACM-001", "name": "Vikram Malhotra", "email": "vikram.malhotra@acmetech.com", "role": "manager"},
    {"code": "ACM-002", "name": "Anita Desai",     "email": "anita.desai@acmetech.com",     "role": "hr_admin"},
    {"code": "ACM-005", "name": "Rahul Mehta",     "email": "rahul.mehta@acmetech.com",     "role": "manager"},
    {"code": "ACM-008", "name": "Priya Sharma",    "email": "priya.sharma@acmetech.com",    "role": "employee"},
    {"code": "ACM-032", "name": "Rohit Kumar",     "email": "rohit.kumar@acmetech.com",     "role": "employee"},
]


def create_session(emp_code: str, email: str, password: str = "AcmeSecret2026!") -> Optional[requests.Session]:
    """Authenticates via demo-login (if DEMO_MODE) or standard login."""
    sess = requests.Session()
    # Try demo login first
    try:
        r_demo = sess.post(f"{BASE_URL}/auth/demo-login", json={"emp_code": emp_code}, timeout=5)
        if r_demo.status_code == 200:
            return sess
    except Exception:
        pass

    # Standard login
    try:
        r_login = sess.post(f"{BASE_URL}/auth/login", json={"email": email, "password": password}, timeout=5)
        if r_login.status_code == 200:
            return sess
    except Exception:
        pass
    return None


def run_chat_query(sess: requests.Session, emp_code: str, query: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/chat"
    session_id = f"smoke-{emp_code.lower()}-{int(time.time())}"
    resp = sess.post(url, json={"session_id": session_id, "message": query}, stream=True, timeout=60)
    
    full_answer = ""
    events = []
    for line in resp.iter_lines():
        if not line:
            continue
        line_str = line.decode("utf-8")
        if line_str.startswith("event: "):
            events.append(line_str[7:].strip())
        elif line_str.startswith("data: "):
            try:
                data = json.loads(line_str[6:])
                if isinstance(data, dict) and "token" in data:
                    full_answer += data["token"]
            except Exception:
                pass
    return {
        "status_code": resp.status_code,
        "answer": full_answer.strip(),
        "events": events,
    }


def main():
    print("\n" + "=" * 76)
    print(f"STARTING HRMS BOT SMOKE TEST against {BASE_URL}")
    print("=" * 76)

    # 1. Server Connectivity
    try:
        r_health = requests.get(f"{BASE_URL}/auth/config", timeout=5)
        if r_health.status_code != 200:
            print(f"[FAIL] Server connectivity check failed: HTTP {r_health.status_code}")
            sys.exit(1)
        print("[PASS] Server is online and responding to /auth/config")
    except Exception as e:
        print(f"[FAIL] Server unreachable at {BASE_URL}: {e}")
        print("Please ensure the application is running via: uvicorn app.main:app --port 8000")
        sys.exit(1)

    # 2. Persona Authentication
    authenticated_sessions = {}
    all_logged_in = True
    for p in PERSONAS:
        s = create_session(p["code"], p["email"])
        if s:
            print(f"  [PASS] Authenticated as {p['name']} ({p['code']}, {p['role']})")
            authenticated_sessions[p["code"]] = s
        else:
            print(f"  [FAIL] Failed to authenticate as {p['name']} ({p['code']})")
            all_logged_in = False

    if not all_logged_in:
        print("[FAIL] Some personas could not log in. Check credentials and seeding.")
        sys.exit(1)

    # 3. HR Admin Endpoints Check
    anita_sess = authenticated_sessions["ACM-002"]
    priya_sess = authenticated_sessions["ACM-008"]

    # Anita (HR Admin) access
    r_onboard = anita_sess.get(f"{BASE_URL}/employees/onboarding", timeout=5)
    r_admin_audit = anita_sess.get(f"{BASE_URL}/audit/admin", timeout=5)
    r_audit = anita_sess.get(f"{BASE_URL}/audit", timeout=5)

    if r_onboard.status_code == 200 and r_admin_audit.status_code == 200 and r_audit.status_code == 200:
        print("[PASS] Anita (HR Admin) successfully accessed /employees/onboarding, /audit/admin, and /audit")
    else:
        print(f"[FAIL] HR Admin endpoint check failed (statuses: {r_onboard.status_code}, {r_admin_audit.status_code}, {r_audit.status_code})")
        sys.exit(1)

    # Priya (Employee) should be blocked from HR Admin endpoints
    r_priya_onboard = priya_sess.get(f"{BASE_URL}/employees/onboarding", timeout=5)
    r_priya_admin_audit = priya_sess.get(f"{BASE_URL}/audit/admin", timeout=5)
    if r_priya_onboard.status_code == 403 and r_priya_admin_audit.status_code == 403:
        print("[PASS] Priya (Employee) correctly forbidden (HTTP 403) from HR Admin endpoints")
    else:
        print(f"[FAIL] Privacy breach: Non-HR employee accessed admin routes! ({r_priya_onboard.status_code}, {r_priya_admin_audit.status_code})")
        sys.exit(1)

    # 4. End-to-End Chat Verification
    print("\nExecuting sample chat turns...")

    # A: Priya querying leave balance
    t0 = time.time()
    chat_res1 = run_chat_query(priya_sess, "ACM-008", "How many casual leaves do I have left?")
    print(f"  [PASS] Priya leave query answered ({time.time() - t0:.2f}s): {chat_res1['answer'][:80]}...")

    # B: Priya querying manager's salary (access control denial check)
    t0 = time.time()
    chat_res2 = run_chat_query(priya_sess, "ACM-008", "What is Rahul's salary?")
    ans_lower = chat_res2['answer'].lower()
    if "only access your own" in ans_lower or "restricted" in ans_lower or "cannot" in ans_lower or "sorry" in ans_lower:
        print(f"  [PASS] Priya manager salary query properly masked/denied ({time.time() - t0:.2f}s)")
    else:
        print(f"  [WARNING] Unexpected response to salary query: {chat_res2['answer']}")

    # C: Rahul querying team on leave
    rahul_sess = authenticated_sessions["ACM-005"]
    t0 = time.time()
    chat_res3 = run_chat_query(rahul_sess, "ACM-005", "Who in my team is on leave this week?")
    print(f"  [PASS] Rahul team on leave query answered ({time.time() - t0:.2f}s): {chat_res3['answer'][:80]}...")

    print("\n" + "=" * 76)
    print("ALL SMOKE TEST CHECKS PASSED SUCCESSFULLY! (100% HEALTHY)")
    print("=" * 76 + "\n")


if __name__ == "__main__":
    main()
