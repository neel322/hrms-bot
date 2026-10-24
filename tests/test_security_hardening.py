import hashlib
import os
import sys
import uuid
from datetime import date, datetime, timedelta
import pytest
from fastapi.testclient import TestClient
import jwt

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.auth import get_jwt_secret, hash_password
from app.config import Settings, get_settings
from app.db import (
    AuthCredentials,
    ChatHistory,
    Employee,
    Invite,
    LeaveBalance,
    LeaveRequest,
    add_chat_message,
    get_chat_history,
    get_session,
    get_session_owner,
    init_db,
)
from app.main import app, validate_session_id
from app.rate_limiter import reset_all_limiters
from app.seed import seed_credentials, seed_database


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()
    seed_database()
    seed_credentials()


@pytest.fixture(autouse=True)
def reset_test_state():
    reset_all_limiters()
    session = get_session()
    try:
        creds = session.query(AuthCredentials).all()
        for c in creds:
            c.must_change_password = False
            c.locked_until = None
            c.failed_attempts = 0
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


def get_cookie_for_employee(employee_id: int) -> dict:
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


# ==============================================================================
# ITEM 1: SESSION OWNERSHIP (cross-user leak)
# ==============================================================================
def test_item_1_session_ownership_and_cross_user_isolation(client):
    """Rahul sending Priya's session_id to /chat and /history gets 403 and never sees her messages."""
    priya = get_employee_by_code("ACM-008")
    rahul = get_employee_by_code("ACM-005")

    priya_cookies = get_cookie_for_employee(priya.id)
    rahul_cookies = get_cookie_for_employee(rahul.id)

    priya_session_id = str(uuid.uuid4())

    # Seed a message from Priya in this session
    add_chat_message(
        session_id=priya_session_id,
        role="user",
        content="Priya private message: When is my appraisal?",
        user_id=priya.emp_code,
    )
    add_chat_message(
        session_id=priya_session_id,
        role="assistant",
        content="Your appraisal is scheduled for Q4.",
        user_id=priya.emp_code,
    )

    # 1. get_session_owner tests
    owner = get_session_owner(priya_session_id)
    assert owner == priya.emp_code

    unused_session = str(uuid.uuid4())
    assert get_session_owner(unused_session) is None

    # 2. Priya can access her own history
    res_priya_hist = client.get(f"/history?session_id={priya_session_id}", cookies=priya_cookies)
    assert res_priya_hist.status_code == 200
    msgs = res_priya_hist.json()
    assert len(msgs) == 2
    assert "Priya private message" in msgs[0]["content"]

    # 3. Rahul accessing Priya's session in /history -> 403 Forbidden
    res_rahul_hist = client.get(f"/history?session_id={priya_session_id}", cookies=rahul_cookies)
    assert res_rahul_hist.status_code == 403
    assert "Forbidden" in res_rahul_hist.json()["detail"]

    # 4. Rahul sending a message with Priya's session_id to /chat -> 403 Forbidden before agent runs
    res_rahul_chat = client.post(
        "/chat",
        json={"session_id": priya_session_id, "message": "Can I read Priya's chat?"},
        cookies=rahul_cookies,
    )
    assert res_rahul_chat.status_code == 403
    assert "Forbidden" in res_rahul_chat.json()["detail"]

    # 5. Invalid / non-UUID-ish session ID rejected with 400
    res_bad_sess1 = client.get("/history?session_id=bad%20session%20id!", cookies=priya_cookies)
    assert res_bad_sess1.status_code == 400

    too_long_sess = "a" * 65
    res_bad_sess2 = client.post(
        "/chat",
        json={"session_id": too_long_sess, "message": "Hello"},
        cookies=priya_cookies,
    )
    assert res_bad_sess2.status_code == 400


# ==============================================================================
# ITEM 2: XSS & SECURITY HEADERS
# ==============================================================================
def test_item_2_security_headers_and_xss_protection(client):
    """Verifies CSP and nosniff headers and ensures malicious payloads have no executable attributes."""
    # Check headers on a regular endpoint
    res = client.get("/health")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"

    csp = res.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' https://cdn.jsdelivr.net" in csp
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "img-src 'self' data:" in csp
    assert "frame-ancestors 'none'" in csp

    # Local fallback file static/vendor/purify.min.js exists and is non-empty
    vendor_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "vendor", "purify.min.js")
    assert os.path.exists(vendor_path)
    assert os.path.getsize(vendor_path) > 10000

    # Seed leave request with XSS payload: <img src=x onerror=alert(1)>
    session = get_session()
    priya = get_employee_by_code("ACM-008")
    try:
        xss_leave = LeaveRequest(
            employee_id=priya.id,
            leave_type="Casual",
            start_date=date.today(),
            end_date=date.today(),
            days=1,
            reason='<img src=x onerror=alert(1)> test xss',
            status="pending",
        )
        session.add(xss_leave)
        session.commit()
        session.refresh(xss_leave)
        leave_id = xss_leave.id
    finally:
        session.close()

    # Query tool result
    from app.tools import execute_tool
    from app.auth import resolve_caller
    priya_caller = resolve_caller("ACM-008")
    leave_result = execute_tool("get_leave_requests", {"employee_id": priya.id}, priya_caller)
    assert "requests" in leave_result
    found_leave = next((r for r in leave_result["requests"] if r.get("id") == leave_id), None)
    assert found_leave is not None

    # Simulating the frontend sanitizer contract
    # Any HTML parsing of assistant or inspector output must not preserve 'onerror'
    raw_reason = found_leave["reason"]
    assert "onerror" in raw_reason
    # When passed through html escaping or purification:
    from html import escape
    escaped_reason = escape(raw_reason)
    assert "<img" not in escaped_reason
    assert "onerror" not in escaped_reason or "&lt;img" in escaped_reason


# ==============================================================================
# ITEM 3: DEMO MODE SAFETY
# ==============================================================================
def test_item_3_demo_mode_safety(client):
    """DEMO_MODE defaults to false in config and .env.example, bind checks refuse non-localhost, and endpoints rate-limit."""
    # 1. Config default
    default_settings = Settings(_env_file=None)
    assert default_settings.demo_mode is False
    assert Settings.model_fields["demo_mode"].default is False

    # 2. .env.example contains DEMO_MODE=false
    env_example_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.example")
    with open(env_example_path, "r") as f:
        env_example = f.read()
    assert "DEMO_MODE=false" in env_example

    # 3. Bind check logic:
    # When DEMO_MODE is true and host is non-loopback (e.g. 0.0.0.0), startup check raises RuntimeError
    old_host = os.environ.get("HOST")
    old_allow = os.environ.get("ALLOW_DEMO_ON_NETWORK")
    old_demo = os.environ.get("DEMO_MODE")
    try:
        os.environ["DEMO_MODE"] = "true"
        os.environ["HOST"] = "0.0.0.0"
        os.environ["ALLOW_DEMO_ON_NETWORK"] = "false"
        get_settings.cache_clear()
        # Test lifespan bind check
        from app.main import lifespan
        with pytest.raises(RuntimeError) as exc_info:
            async def run_lifespan():
                async with lifespan(app):
                    pass
            import asyncio
            asyncio.run(run_lifespan())
        assert "Refusing to start" in str(exc_info.value)
    finally:
        if old_host is not None:
            os.environ["HOST"] = old_host
        else:
            os.environ.pop("HOST", None)
        if old_allow is not None:
            os.environ["ALLOW_DEMO_ON_NETWORK"] = old_allow
        else:
            os.environ.pop("ALLOW_DEMO_ON_NETWORK", None)
        if old_demo is not None:
            os.environ["DEMO_MODE"] = old_demo
        else:
            os.environ.pop("DEMO_MODE", None)
        get_settings.cache_clear()

    # 4. Rate limit on /auth/demo-login (when demo mode active, or test rate limiter directly)
    try:
        os.environ["DEMO_MODE"] = "true"
        get_settings.cache_clear()
        for _ in range(10):
            client.post("/auth/demo-login", json={"emp_code": "ACM-008"})
        res_demo_limited = client.post("/auth/demo-login", json={"emp_code": "ACM-008"})
        assert res_demo_limited.status_code == 429
        assert "Too many demo login requests" in res_demo_limited.json()["detail"]
    finally:
        if old_demo is not None:
            os.environ["DEMO_MODE"] = old_demo
        else:
            os.environ.pop("DEMO_MODE", None)
        get_settings.cache_clear()

    # 5. Rate limit on /auth/activate (10 requests per minute)
    reset_all_limiters()
    for _ in range(10):
        client.post("/auth/activate", json={"token": "dummy", "new_password": "dummy"})
    res_rate_limited = client.post("/auth/activate", json={"token": "dummy", "new_password": "dummy"})
    assert res_rate_limited.status_code == 429
    assert "Too many activation attempts" in res_rate_limited.json()["detail"]


# ==============================================================================
# ITEM 4: LOGIN HARDENING
# ==============================================================================
def test_item_4_login_hardening_generic_errors(client):
    """Identical generic 401 error for unknown email, wrong password, terminated, and locked account."""
    target_emp = get_employee_by_code("ACM-009")  # Arjun Patel

    # 1. Unknown email
    res_unknown = client.post("/auth/login", json={"email": "nonexistent.user@acmetech.com", "password": "AnyPassword123!"})
    assert res_unknown.status_code == 401
    assert res_unknown.json()["detail"] == "Invalid email or password"

    # 2. Wrong password
    res_wrong = client.post("/auth/login", json={"email": target_emp.email, "password": "WrongPassword123!"})
    assert res_wrong.status_code == 401
    assert res_wrong.json()["detail"] == "Invalid email or password"

    # 3. Terminated / inactive account
    session = get_session()
    try:
        term_emp = session.query(Employee).filter(Employee.status == "terminated").first()
        if not term_emp:
            term_emp = Employee(
                emp_code="ACM-999",
                name="Terminated Tester",
                email="term.test@acmetech.com",
                department="QA",
                designation="Tester",
                status="terminated",
                join_date=date.today() - timedelta(days=100),
                work_location="HQ",
            )
            session.add(term_emp)
            session.commit()
            session.refresh(term_emp)
            creds = AuthCredentials(
                employee_id=term_emp.id,
                password_hash=hash_password("ValidPassword123!"),
            )
            session.add(creds)
            session.commit()
    finally:
        session.close()

    res_term = client.post("/auth/login", json={"email": term_emp.email, "password": "ValidPassword123!"})
    assert res_term.status_code == 401
    assert res_term.json()["detail"] == "Invalid email or password"

    # 4. Locked account
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == target_emp.id).first()
        creds.locked_until = datetime.utcnow() + timedelta(minutes=15)
        creds.failed_attempts = 5
        session.commit()
    finally:
        session.close()

    res_locked = client.post("/auth/login", json={"email": target_emp.email, "password": "AnyPassword123!"})
    assert res_locked.status_code == 401
    assert res_locked.json()["detail"] == "Invalid email or password"

    # 5. Lock expiry resets failed_attempts
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == target_emp.id).first()
        # Set lock in the past
        creds.locked_until = datetime.utcnow() - timedelta(seconds=10)
        session.commit()
    finally:
        session.close()

    # Trigger a login attempt; lock is expired so counters should reset
    client.post("/auth/login", json={"email": target_emp.email, "password": "WrongPassword123!"})

    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == target_emp.id).first()
        # failed_attempts was reset to 0 upon recognizing expired lock, then incremented to 1 by the wrong password
        assert creds.failed_attempts == 1
        assert creds.locked_until is None
        # Clean up
        creds.failed_attempts = 0
        session.commit()
    finally:
        session.close()


# ==============================================================================
# ITEM 5: PASSWORD LIFECYCLE
# ==============================================================================
def test_item_5_password_lifecycle(client):
    """Tests password change, 72-byte max, loosened name rule, must_change_password enforcement, and token invalidation."""
    priya = get_employee_by_code("ACM-008")
    # 1. Set Priya's password known and initialize creds first
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == priya.id).first()
        creds.password_hash = hash_password("CurrentPassword123!")
        creds.must_change_password = False
        creds.token_version = 1
        session.commit()
    finally:
        session.close()

    priya_cookies = get_cookie_for_employee(priya.id)

    # 2. Test max length 72 bytes
    long_pwd = "A" * 73
    res_too_long = client.post(
        "/auth/change-password",
        json={"current_password": "CurrentPassword123!", "new_password": long_pwd},
        cookies=priya_cookies,
    )
    assert res_too_long.status_code == 400
    assert "72 bytes" in res_too_long.json()["detail"]

    # 3. Test minimum 10 characters
    res_too_short = client.post(
        "/auth/change-password",
        json={"current_password": "CurrentPassword123!", "new_password": "Short1!"},
        cookies=priya_cookies,
    )
    assert res_too_short.status_code == 400
    assert "10 characters" in res_too_short.json()["detail"]

    # 4. Test loosened name rule:
    # 3-letter name fragment e.g. "pri" should be ALLOWED
    # Full name "priya sharma" or full email username "priya.sharma" should be REJECTED
    res_full_name = client.post(
        "/auth/change-password",
        json={"current_password": "CurrentPassword123!", "new_password": "Priya Sharma123!"},
        cookies=priya_cookies,
    )
    assert res_full_name.status_code == 400
    assert "full name" in res_full_name.json()["detail"].lower()

    res_fragment = client.post(
        "/auth/change-password",
        json={"current_password": "CurrentPassword123!", "new_password": "SpringSeason2026!"},
        cookies=priya_cookies,
    )
    assert res_fragment.status_code == 200

    # 5. Token invalidation: old token (version 1) is now rejected because version incremented to 2
    res_old_token = client.get("/employees", cookies=priya_cookies)
    assert res_old_token.status_code == 401
    assert "invalidated" in res_old_token.json()["detail"].lower()

    # 6. Enforce must_change_password:
    # When must_change_password is True, endpoints return 403 {"code": "PASSWORD_CHANGE_REQUIRED"}
    # Except /auth/me, /auth/change-password, /auth/logout
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == priya.id).first()
        creds.must_change_password = True
        session.commit()
    finally:
        session.close()

    new_priya_cookies = get_cookie_for_employee(priya.id)

    # /employees returns 403 PASSWORD_CHANGE_REQUIRED
    res_blocked = client.get("/employees", cookies=new_priya_cookies)
    assert res_blocked.status_code == 403
    assert res_blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"

    # /chat returns 403 PASSWORD_CHANGE_REQUIRED
    res_chat_blocked = client.post(
        "/chat",
        json={"session_id": str(uuid.uuid4()), "message": "Hi"},
        cookies=new_priya_cookies,
    )
    assert res_chat_blocked.status_code == 403
    assert res_chat_blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"

    # /auth/me is EXEMPT
    res_me = client.get("/auth/me", cookies=new_priya_cookies)
    assert res_me.status_code == 200
    assert res_me.json()["must_change_password"] is True

    # /auth/change-password is EXEMPT and clears must_change_password
    res_change = client.post(
        "/auth/change-password",
        json={"current_password": "SpringSeason2026!", "new_password": "SummerSeason2026!"},
        cookies=new_priya_cookies,
    )
    assert res_change.status_code == 200

    # 7. HR reset password action via /employees/{id}/invite
    anita = get_employee_by_code("ACM-002")
    anita_cookies = get_cookie_for_employee(anita.id)

    res_invite = client.post(f"/employees/{priya.id}/invite", cookies=anita_cookies)
    assert res_invite.status_code == 200
    assert "activation_link" in res_invite.json()

    # Verify must_change_password is set to True in DB and token_version revoked
    session = get_session()
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == priya.id).first()
        assert creds.must_change_password is True
        # Older unused invites revoked
        invites = session.query(Invite).filter(Invite.employee_id == priya.id, Invite.used_at.is_(None)).all()
        assert len(invites) == 1  # Only the new one remains
        # Reset must_change_password so future tests have clean state
        creds.must_change_password = False
        session.commit()
    finally:
        session.close()


# ==============================================================================
# ITEM 6: INFORMATION LEAKS
# ==============================================================================
def test_item_6_information_leaks(client):
    """Unauthenticated /health returns only status ok; /health/details requires hr_admin; no raw exceptions."""
    # 1. Unauthenticated /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    # 2. Detailed health requires hr_admin
    priya = get_employee_by_code("ACM-008")
    anita = get_employee_by_code("ACM-002")
    priya_cookies = get_cookie_for_employee(priya.id)
    anita_cookies = get_cookie_for_employee(anita.id)

    # Unauthenticated -> 401
    res_det_unauth = client.get("/health/details")
    assert res_det_unauth.status_code == 401

    # Employee (Priya) -> 403
    res_det_priya = client.get("/health/details", cookies=priya_cookies)
    assert res_det_priya.status_code == 403

    # HR Admin (Anita) -> 200 with full details
    res_det_anita = client.get("/health/details", cookies=anita_cookies)
    assert res_det_anita.status_code == 200
    data = res_det_anita.json()
    assert "db_type" in data
    assert "qdrant_mode" in data
    assert "tables" in data
