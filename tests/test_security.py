import io
import json
import os
import sys
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
import jwt

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.auth import get_jwt_secret, hash_password
from app.config import get_settings
from app.db import (
    AuthCredentials,
    ChatHistory,
    Employee,
    add_chat_message,
    get_session,
    init_db,
)
from app.main import app
from app.rate_limiter import reset_all_limiters
from app.seed import seed_credentials, seed_database


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Initializes tables and seeds credentials once for testing."""
    init_db()
    seed_database()
    seed_credentials()


@pytest.fixture(autouse=True)
def reset_limiters():
    """Resets rate limiters before each test run."""
    reset_all_limiters()


@pytest.fixture
def client():
    return TestClient(app)


def get_cookie_for_employee(employee_id: int) -> dict:
    """Helper to generate a valid signed access_token cookie for an employee."""
    secret = get_jwt_secret()
    exp = datetime.utcnow() + timedelta(hours=8)
    session = get_session()
    token_v = 1
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == employee_id).first()
        if creds:
            token_v = creds.token_version
    finally:
        session.close()
    token = jwt.encode({"employee_id": employee_id, "token_version": token_v, "exp": exp}, secret, algorithm="HS256")
    return {"access_token": token}


def get_employee_by_code(emp_code: str) -> Employee:
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.emp_code == emp_code).first()
        assert emp is not None, f"Employee {emp_code} not found in test database"
        return emp
    finally:
        session.close()


# ---------------------------------------------------------------------------
# TEST 1: /chat without a cookie -> 401
# ---------------------------------------------------------------------------
def test_1_chat_without_cookie_returns_401(client):
    client.cookies.clear()
    response = client.post(
        "/chat",
        json={"session_id": "test_anon_session", "message": "What is the leave policy?"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


# ---------------------------------------------------------------------------
# TEST 2: Log in as Priya, send a body with another user's user_id -> still answers as Priya
# ---------------------------------------------------------------------------
def test_2_client_supplied_user_id_ignored_in_chat(client):
    priya = get_employee_by_code("ACM-008")
    cookies = get_cookie_for_employee(priya.id)

    # Send a request where the client attempts to override identity to Rahul (ACM-005)
    response = client.post(
        "/chat",
        cookies=cookies,
        json={
            "session_id": "sess_priya_spoof_test",
            "message": "Hello from Priya",
            "user_id": "ACM-005",  # Client-supplied spoofed identity
        },
    )
    assert response.status_code == 200

    # Parse SSE stream chunks to verify the context_info event identified Priya
    stream_content = response.text
    assert "event: context_info" in stream_content

    # Extract JSON inside data: for context_info
    found_priya_context = False
    for line in stream_content.splitlines():
        if line.startswith("data: ") and '"emp_code": "ACM-008"' in line:
            data = json.loads(line[6:])
            assert data["caller"]["emp_code"] == "ACM-008"
            assert data["caller"]["name"] == "Priya Sharma"
            found_priya_context = True
            break

    assert found_priya_context, "Expected context_info to identify caller as Priya (ACM-008), ignoring ACM-005 body override"


# ---------------------------------------------------------------------------
# TEST 3: user "%" or "%a%" cannot log in
# ---------------------------------------------------------------------------
def test_3_wildcard_users_cannot_login(client):
    # Test POST /auth/login with SQL wildcard characters
    res1 = client.post("/auth/login", json={"email": "%", "password": "password123"})
    assert res1.status_code == 401
    assert res1.json()["detail"] == "Invalid email or password"

    res2 = client.post("/auth/login", json={"email": "%a%", "password": "password123"})
    assert res2.status_code == 401
    assert res2.json()["detail"] == "Invalid email or password"

    res3 = client.post("/auth/login", json={"email": "_", "password": "password123"})
    assert res3.status_code == 401

    # Test POST /auth/demo-login with wildcard
    res_demo = client.post("/auth/demo-login", json={"emp_code": "%"})
    assert res_demo.status_code == 404


# ---------------------------------------------------------------------------
# TEST 4: Priya GET /audit -> 403; Priya POST /ingest -> 403; Anita -> allowed
# ---------------------------------------------------------------------------
def test_4_role_based_access_audit_and_ingest(client):
    priya = get_employee_by_code("ACM-008")  # role: employee
    anita = get_employee_by_code("ACM-002")  # role: hr_admin

    priya_cookies = get_cookie_for_employee(priya.id)
    anita_cookies = get_cookie_for_employee(anita.id)

    # Priya attempts GET /audit -> 403
    priya_audit = client.get("/audit", cookies=priya_cookies)
    assert priya_audit.status_code == 403
    assert "Forbidden" in priya_audit.json()["detail"]

    # Priya attempts POST /ingest -> 403
    priya_ingest = client.post(
        "/ingest",
        cookies=priya_cookies,
        files={"file": ("test.txt", io.BytesIO(b"Employee handbook"), "text/plain")},
    )
    assert priya_ingest.status_code == 403
    assert "Forbidden" in priya_ingest.json()["detail"]

    # Anita (HR Admin) attempts GET /audit -> allowed (200)
    anita_audit = client.get("/audit", cookies=anita_cookies)
    assert anita_audit.status_code == 200
    assert "logs" in anita_audit.json()

    # Anita (HR Admin) attempts POST /ingest -> allowed (200)
    anita_ingest = client.post(
        "/ingest",
        cookies=anita_cookies,
        files={"file": ("hr_memo.txt", io.BytesIO(b"HR Administrative Policy Update"), "text/plain")},
    )
    assert anita_ingest.status_code == 200
    assert anita_ingest.json()["chunks_ingested"] >= 0


# ---------------------------------------------------------------------------
# TEST 5: Priya GET /history for Rahul's session -> 403/404
# ---------------------------------------------------------------------------
def test_5_history_session_ownership_enforcement(client):
    priya = get_employee_by_code("ACM-008")
    rahul = get_employee_by_code("ACM-005")

    priya_cookies = get_cookie_for_employee(priya.id)

    # Seed a private session message for Rahul
    rahul_session_id = "sess_rahul_confidential_99"
    add_chat_message(
        session_id=rahul_session_id,
        role="user",
        content="Manager question about salary bands",
        user_id=rahul.emp_code,
    )

    # Priya attempts to access Rahul's session history -> 403
    res_priya = client.get(
        "/history",
        cookies=priya_cookies,
        params={"session_id": rahul_session_id},
    )
    assert res_priya.status_code == 403
    assert "Forbidden" in res_priya.json()["detail"]

    # Non-existent session -> 404
    res_nonexistent = client.get(
        "/history",
        cookies=priya_cookies,
        params={"session_id": "non_existent_session_xyz_123"},
    )
    assert res_nonexistent.status_code == 404


# ---------------------------------------------------------------------------
# TEST 6: Priya GET /memories only returns her own
# ---------------------------------------------------------------------------
def test_6_memories_ownership_enforcement(client):
    priya = get_employee_by_code("ACM-008")
    priya_cookies = get_cookie_for_employee(priya.id)

    # Priya querying without user_id -> allowed, returns 200
    res_own = client.get("/memories", cookies=priya_cookies)
    assert res_own.status_code == 200
    memories = res_own.json()
    for m in memories:
        assert m["user_id"] == priya.emp_code

    # Priya querying another employee's user_id -> 403 Forbidden
    res_other = client.get("/memories", cookies=priya_cookies, params={"user_id": "ACM-005"})
    assert res_other.status_code == 403
    assert "Forbidden" in res_other.json()["detail"]

    # Priya deleting another employee's memories -> 403 Forbidden
    res_del_other = client.delete("/memories", cookies=priya_cookies, params={"user_id": "ACM-005"})
    assert res_del_other.status_code == 403


# ---------------------------------------------------------------------------
# TEST 7: 6 wrong passwords -> account locked
# ---------------------------------------------------------------------------
def test_7_account_locks_after_failed_password_attempts(client):
    target_emp = get_employee_by_code("ACM-009")  # Arjun Patel
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == target_emp.id).first()
        assert creds is not None
        # Reset any prior failed attempts
        creds.failed_attempts = 0
        creds.locked_until = None
        session.commit()
    finally:
        session.close()

    # Attempt 5 wrong passwords: each returns 401
    for attempt in range(1, 6):
        res = client.post(
            "/auth/login",
            json={"email": target_emp.email, "password": f"WrongPass{attempt}!"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid email or password"

    # Attempt 6: Account should now be locked (HTTP 401 with generic error)
    res_locked = client.post(
        "/auth/login",
        json={"email": target_emp.email, "password": "WrongPass6!"},
    )
    assert res_locked.status_code == 401
    assert res_locked.json()["detail"] == "Invalid email or password"

    # Verify locked_until is set in the database
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == target_emp.id).first()
        assert creds.locked_until is not None
        assert creds.locked_until > datetime.utcnow()
        # Clean up so Arjun's account isn't locked for future runs
        creds.failed_attempts = 0
        creds.locked_until = None
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# TEST 8: A terminated employee (status='terminated') cannot log in or chat
# ---------------------------------------------------------------------------
def test_8_terminated_employee_cannot_login_or_chat(client):
    term_emp = get_employee_by_code("ACM-031")
    assert term_emp.status == "terminated"

    # Terminated employee cannot log in via /auth/login
    res_login = client.post(
        "/auth/login",
        json={"email": term_emp.email, "password": "AcmeSecret2026!"},
    )
    assert res_login.status_code in (401, 403)
    assert "terminated" in res_login.json()["detail"] or "Invalid" in res_login.json()["detail"]

    # Terminated employee cannot use /auth/demo-login (returns 403 when DEMO_MODE=true or 404 when disabled)
    res_demo = client.post("/auth/demo-login", json={"emp_code": term_emp.emp_code})
    assert res_demo.status_code in (401, 403, 404)

    # Even with a forged/active cookie, /chat rejects terminated employee via get_current_caller
    forged_cookie = get_cookie_for_employee(term_emp.id)
    res_chat = client.post(
        "/chat",
        cookies=forged_cookie,
        json={"session_id": "test_term_session", "message": "Can I chat?"},
    )
    assert res_chat.status_code == 401
    assert "terminated" in res_chat.json()["detail"]


# ---------------------------------------------------------------------------
# TEST 9: Uploading a file named leave_policy.md as non-HR is blocked
# ---------------------------------------------------------------------------
def test_9_non_hr_uploading_policy_blocked(client):
    priya = get_employee_by_code("ACM-008")  # IC employee
    priya_cookies = get_cookie_for_employee(priya.id)

    response = client.post(
        "/ingest",
        cookies=priya_cookies,
        files={"file": ("leave_policy.md", io.BytesIO(b"# Malicious Policy Overwrite"), "text/markdown")},
    )
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"]


# ---------------------------------------------------------------------------
# ADDITIONAL TESTS: Directory Privacy, Ingest Hardening, and Logout
# ---------------------------------------------------------------------------
def test_employees_directory_privacy(client):
    priya = get_employee_by_code("ACM-008")
    anita = get_employee_by_code("ACM-002")

    # Regular employee gets minimal directory
    res_priya = client.get("/employees", cookies=get_cookie_for_employee(priya.id))
    assert res_priya.status_code == 200
    priya_dir = res_priya.json()
    for e in priya_dir:
        assert "name" in e and e["name"]
        assert "department" in e and e["department"]
        assert "designation" in e and e["designation"]
        # Sensitive fields must be hidden / None
        assert e["email"] is None
        assert e["emp_code"] is None
        assert e["role"] is None

    # HR Admin gets full directory
    res_anita = client.get("/employees", cookies=get_cookie_for_employee(anita.id))
    assert res_anita.status_code == 200
    anita_dir = res_anita.json()
    first = anita_dir[0]
    assert first["email"] is not None
    assert first["emp_code"] is not None
    assert first["role"] is not None


def test_ingest_hardening_rules(client):
    anita = get_employee_by_code("ACM-002")
    anita_cookies = get_cookie_for_employee(anita.id)

    # Disallow unsupported file formats (e.g. .exe or .py)
    res_bad_format = client.post(
        "/ingest",
        cookies=anita_cookies,
        files={"file": ("malware.exe", io.BytesIO(b"executable data"), "application/octet-stream")},
    )
    assert res_bad_format.status_code == 400

    # Max size enforcement (>10MB)
    large_payload = b"X" * (10 * 1024 * 1024 + 1024)
    res_oversized = client.post(
        "/ingest",
        cookies=anita_cookies,
        files={"file": ("large.txt", io.BytesIO(large_payload), "text/plain")},
    )
    assert res_oversized.status_code in (413, 400)


def test_logout_endpoint(client):
    res = client.post("/auth/logout")
    assert res.status_code == 200
    assert res.json()["message"] == "Logged out successfully"
    # access_token cookie deleted or set to empty
    cookie = res.headers.get("set-cookie", "")
    assert "access_token" in cookie
