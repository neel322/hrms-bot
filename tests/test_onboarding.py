import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
import pytest
from fastapi.testclient import TestClient
import jwt

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.auth import get_jwt_secret, hash_password
from app.config import get_settings
from app.db import (
    AuthCredentials,
    Employee,
    Invite,
    LeaveBalance,
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


# ==============================================================================
# Scenario 1: Priya (employee) POST /employees -> 403
# ==============================================================================
def test_regular_employee_cannot_create_employee(client):
    """Priya Sharma (employee role) must receive 403 Forbidden when trying to create an employee."""
    priya = get_employee_by_code("ACM-008")
    cookies = get_cookie_for_employee(priya.id)

    payload = {
        "name": "Unauthorized Joiner",
        "work_email": "unauth@acmetech.com",
        "department": "Engineering",
        "designation": "Software Engineer",
        "join_date": date.today().isoformat(),
        "work_location": "Headquarters - Bangalore",
        "role": "employee",
    }
    response = client.post("/employees", json=payload, cookies=cookies)
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"] or "HR Admin" in response.json()["detail"]


# ==============================================================================
# Scenario 2: Anita creates a new employee -> has emp_code, leave balances prorated, status onboarding
# ==============================================================================
def test_anita_creates_new_employee_successfully(client):
    """Anita Desai (hr_admin) creates an employee with prorated leave balances and status 'onboarding'."""
    anita = get_employee_by_code("ACM-002")
    cookies = get_cookie_for_employee(anita.id)

    today = date.today()
    unique_suffix = datetime.utcnow().strftime("%f")
    unique_email = f"test.joiner.{unique_suffix}@acmetech.com"

    payload = {
        "name": "Tanvi Joshi",
        "work_email": unique_email,
        "department": "Engineering",
        "designation": "Associate Backend Engineer",
        "join_date": today.isoformat(),
        "work_location": "Headquarters - Bangalore",
        "role": "employee",
        "manager_id": 5,  # Rahul Mehta
    }
    response = client.post("/employees", json=payload, cookies=cookies)
    assert response.status_code == 201
    data = response.json()

    emp_data = data["employee"]
    assert emp_data["name"] == "Tanvi Joshi"
    assert emp_data["email"] == unique_email
    assert emp_data["emp_code"].startswith("ACM-")
    assert emp_data["status"] == "onboarding"

    # Verify prorated leave balances returned
    balances = data["leave_balances"]
    assert len(balances) == 3
    leave_types = {b["leave_type"] for b in balances}
    assert leave_types == {"Casual", "Sick", "Earned"}

    # Verify in DB
    session = get_session()
    try:
        db_emp = session.query(Employee).filter(Employee.email == unique_email).first()
        assert db_emp is not None
        assert db_emp.status == "onboarding"
        assert db_emp.probation_end_date is not None

        db_balances = session.query(LeaveBalance).filter(LeaveBalance.employee_id == db_emp.id).all()
        assert len(db_balances) == 3

        # Verify credentials created with must_change_password=True
        db_creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == db_emp.id).first()
        assert db_creds is not None
        assert db_creds.must_change_password is True
    finally:
        session.close()


# ==============================================================================
# Scenario 3: Duplicate email -> 409
# ==============================================================================
def test_duplicate_work_email_returns_409(client):
    """Creating an employee with an existing email returns 409 Conflict."""
    anita = get_employee_by_code("ACM-002")
    cookies = get_cookie_for_employee(anita.id)

    # Use Priya's existing email
    payload = {
        "name": "Imposter Priya",
        "work_email": "priya.sharma@acmetech.com",
        "department": "Engineering",
        "designation": "Software Engineer",
        "join_date": date.today().isoformat(),
        "work_location": "Headquarters - Bangalore",
        "role": "employee",
    }
    response = client.post("/employees", json=payload, cookies=cookies)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


# ==============================================================================
# Scenario 4: Generate an invite, activate with weak password -> rejected; strong password -> logged in
# ==============================================================================
def test_generate_invite_and_password_validation(client):
    """Tests generating an invite, weak password rejection, and successful activation with strong password."""
    anita = get_employee_by_code("ACM-002")
    anita_cookies = get_cookie_for_employee(anita.id)

    # Create new employee
    unique_suffix = datetime.utcnow().strftime("%f")
    unique_email = f"activate.test.{unique_suffix}@acmetech.com"
    create_res = client.post("/employees", json={
        "name": "Activation Candidate",
        "work_email": unique_email,
        "department": "Engineering",
        "designation": "QA Analyst",
        "join_date": date.today().isoformat(),
        "work_location": "Headquarters - Bangalore",
        "role": "employee",
    }, cookies=anita_cookies)
    assert create_res.status_code == 201
    new_emp_id = create_res.json()["employee"]["id"]

    # Generate invite
    invite_res = client.post(f"/employees/{new_emp_id}/invite", cookies=anita_cookies)
    assert invite_res.status_code == 200
    invite_data = invite_res.json()
    token = invite_data["token"]
    assert token is not None

    # Test Weak Password: < 10 characters
    weak_res = client.post("/auth/activate", json={
        "token": token,
        "new_password": "short",
    })
    assert weak_res.status_code == 400
    assert "at least 10 characters" in weak_res.json()["detail"].lower()

    # Test Weak Password: contains user's full name
    name_res = client.post("/auth/activate", json={
        "token": token,
        "new_password": "Activation CandidateSecret2026!",
    })
    assert name_res.status_code == 400
    assert "name" in name_res.json()["detail"].lower()

    # Test Weak Password: equal to email
    email_res = client.post("/auth/activate", json={
        "token": token,
        "new_password": unique_email,
    })
    assert email_res.status_code == 400
    assert "email" in email_res.json()["detail"].lower()

    # Test Strong Password: >= 10 chars, not name or email
    strong_pwd = "SuperSecurePassword9988!"
    strong_res = client.post("/auth/activate", json={
        "token": token,
        "new_password": strong_pwd,
    })
    assert strong_res.status_code == 200
    assert "Account activated successfully" in strong_res.json()["message"]
    assert "access_token" in strong_res.cookies

    # Verify user can immediately query /auth/me with the set cookie
    activated_cookie = {"access_token": strong_res.cookies["access_token"]}
    me_res = client.get("/auth/me", cookies=activated_cookie)
    assert me_res.status_code == 200
    assert me_res.json()["email"] == unique_email


# ==============================================================================
# Scenario 5: Reusing the same token -> rejected; an expired token -> rejected
# ==============================================================================
def test_token_reuse_and_expiration(client):
    """Tests that a used token cannot be reused, and an expired token is rejected."""
    anita = get_employee_by_code("ACM-002")
    anita_cookies = get_cookie_for_employee(anita.id)

    unique_suffix = datetime.utcnow().strftime("%f")
    unique_email = f"reuse.test.{unique_suffix}@acmetech.com"
    create_res = client.post("/employees", json={
        "name": "Token Tester",
        "work_email": unique_email,
        "department": "Engineering",
        "designation": "Data Analyst",
        "join_date": date.today().isoformat(),
        "work_location": "Headquarters - Bangalore",
    }, cookies=anita_cookies)
    new_emp_id = create_res.json()["employee"]["id"]

    # Generate token
    invite_res = client.post(f"/employees/{new_emp_id}/invite", cookies=anita_cookies)
    token = invite_res.json()["token"]

    # First activation: succeeds
    res1 = client.post("/auth/activate", json={
        "token": token,
        "new_password": "ValidSecretPassword123!",
    })
    assert res1.status_code == 200

    # Second activation (Reuse): must fail
    res2 = client.post("/auth/activate", json={
        "token": token,
        "new_password": "AnotherSecretPassword123!",
    })
    assert res2.status_code == 400
    assert "already been used" in res2.json()["detail"].lower()

    # Create expired token directly in DB
    session = get_session()
    try:
        expired_raw = f"expired-token-{unique_suffix}"
        expired_hash = hashlib.sha256(expired_raw.encode()).hexdigest()
        expired_invite = Invite(
            employee_id=new_emp_id,
            token_hash=expired_hash,
            expires_at=datetime.utcnow() - timedelta(hours=1),  # 1 hour ago
            used_at=None,
        )
        session.add(expired_invite)
        session.commit()
    finally:
        session.close()

    # Attempt activation with expired token
    exp_res = client.post("/auth/activate", json={
        "token": expired_raw,
        "new_password": "ValidSecretPassword123!",
    })
    assert exp_res.status_code == 400
    assert "expired" in exp_res.json()["detail"].lower()


# ==============================================================================
# Scenario 6: As the new joiner:
# - "What do I do on Day 1?" -> cites onboarding_guide.md
# - "Show my payslip" -> friendly empty message
# - "Show Priya's leave balance" -> access denied
# ==============================================================================
def test_new_joiner_chatbot_experience(client):
    """Tests the new joiner experience: Day 1 policy citation, payslip empty state, and access denial."""
    # Seed new joiner Rohit Kumar (ACM-032)
    session = get_session()
    try:
        rohit = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        assert rohit is not None
        rohit_id = rohit.id
    finally:
        session.close()

    rohit_cookies = get_cookie_for_employee(rohit_id)

    # 1. Test tools directly via app.tools:
    from app.auth import resolve_caller
    from app.tools import execute_tool

    rohit_caller = resolve_caller("ACM-032")

    # Day 1 policy search
    policy_res = execute_tool(
        name="search_policies",
        arguments={"query": "What do I do on Day 1 orientation schedule?"},
        caller=rohit_caller,
    )
    assert any("onboarding_guide.md" in r["source"] for r in policy_res.get("results", []))

    # Own payslip -> graceful empty state
    payslip_res = execute_tool(
        name="get_payslip",
        arguments={},
        caller=rohit_caller,
    )
    assert payslip_res.get("empty") is True
    assert "first payroll cycle" in payslip_res.get("reason", "").lower()

    # Query Priya's leave balance -> masked as not found / access denied for privacy
    priya_leave_res = execute_tool(
        name="get_leave_balance",
        arguments={"employee": "Priya Sharma"},
        caller=rohit_caller,
    )
    assert priya_leave_res.get("access_denied") is True or "not found" in priya_leave_res.get("error", "").lower()

    # 2. Also verify through chat endpoint streaming
    chat_res = client.post("/chat", json={
        "session_id": "test_sess_rohit_onboarding",
        "message": "Show Priya's leave balance",
    }, cookies=rohit_cookies)
    assert chat_res.status_code == 200
    body = chat_res.text
    # Reconstruct streamed text from SSE
    import json
    streamed_tokens = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
                if "token" in payload:
                    streamed_tokens.append(payload["token"])
            except Exception:
                pass
    combined_text = (body + " " + "".join(streamed_tokens)).lower()
    terms = [
        "access_denied", "permission", "cannot access", "can only access", "don't have permission",
        "not found", "does not exist", "unable to find", "only view your own", "only access your own",
        "only able to view", "can't provide", "cannot provide", "limited to", "cannot display",
        "only have access", "access to your own"
    ]
    assert any(term in combined_text for term in terms)


# ==============================================================================
# Scenario 7: Offboard the employee -> can no longer log in
# ==============================================================================
def test_offboard_employee_revokes_login(client):
    """Offboarding an employee sets status='terminated', immediately revoking login & chat."""
    anita = get_employee_by_code("ACM-002")
    anita_cookies = get_cookie_for_employee(anita.id)

    # Create employee to offboard
    unique_suffix = datetime.utcnow().strftime("%f")
    unique_email = f"offboard.test.{unique_suffix}@acmetech.com"
    create_res = client.post("/employees", json={
        "name": "Offboard Candidate",
        "work_email": unique_email,
        "department": "Engineering",
        "designation": "Full-Stack Dev",
        "join_date": date.today().isoformat(),
        "work_location": "Headquarters - Bangalore",
    }, cookies=anita_cookies)
    emp_id = create_res.json()["employee"]["id"]

    # Activate employee
    invite_res = client.post(f"/employees/{emp_id}/invite", cookies=anita_cookies)
    token = invite_res.json()["token"]
    act_res = client.post("/auth/activate", json={
        "token": token,
        "new_password": "ValidStrongPassword100!",
    })
    assert act_res.status_code == 200

    # User can log in before offboarding
    login_res = client.post("/auth/login", json={
        "email": unique_email,
        "password": "ValidStrongPassword100!",
    })
    assert login_res.status_code == 200
    emp_cookie = {"access_token": login_res.cookies["access_token"]}

    # Offboard employee as Anita
    offboard_res = client.post(f"/employees/{emp_id}/offboard", json={
        "last_working_date": date.today().isoformat(),
        "reason": "Contract ended",
    }, cookies=anita_cookies)
    assert offboard_res.status_code == 200
    assert offboard_res.json()["status"] == "terminated"

    # Attempt to log in again -> Rejected (403 Account is terminated)
    login_again = client.post("/auth/login", json={
        "email": unique_email,
        "password": "ValidStrongPassword100!",
    })
    assert login_again.status_code == 401
    assert login_again.json()["detail"] == "Invalid email or password"

    # Attempt to make a chat request with previously active token -> Rejected (401 status terminated)
    chat_again = client.post("/chat", json={
        "session_id": "offboard_test_sess",
        "message": "Hello bot",
    }, cookies=emp_cookie)
    assert chat_again.status_code == 401
    assert "terminated" in chat_again.json()["detail"].lower()
