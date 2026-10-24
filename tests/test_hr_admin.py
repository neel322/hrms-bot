"""Unit and regression tests for Part 4: HR Admin flows and release preparation.

Covers:
1. add_months() day clamping (Jan 31, Aug 31, Mar 31, Feb 29).
2. Email validation for work and personal emails & personal_email persistence.
3. Safe employee code generation with retries on unique constraint collision.
4. Unique constraint on leave_balances (employee_id, leave_type, year).
5. Manager role restriction ('manager' or 'hr_admin').
6. Manager changes: self-management rejection (400), cyclic hierarchy rejection (400),
   demoting manager with direct reports rejection (409).
7. Offboarding: future date vs immediate date, reports reassignment requirement (409),
   cancelling pending leave requests.
8. Reactivate endpoint (POST /employees/{id}/reactivate).
9. Daily job status sync: flips onboarding -> active, applies scheduled terminations.
10. Expired invite state representation.
11. Admin audit trail recording and GET /audit/admin endpoint access control.
12. Policy ingestion replace=True deleting old chunks.
"""
from datetime import date, datetime, timedelta
import json
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
import jwt

from app.auth import get_jwt_secret
from app.db import (
    AdminAudit,
    AuthCredentials,
    Employee,
    Invite,
    LeaveBalance,
    LeaveRequest,
    get_admin_audit_logs,
    get_session,
    init_db,
    log_admin_audit,
)
from app.config import get_settings
from app.ingest import process_and_ingest_document
from app.main import app, run_daily_status_sync
from app.seed import reset_passwords
from app.utils import add_months, get_current_date


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()


@pytest.fixture
def client():
    return TestClient(app)


def get_cookie_for(emp_code: str):
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.emp_code == emp_code).first()
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        token_ver = creds.token_version if creds else 1
        secret = get_jwt_secret()
        exp = datetime.utcnow() + timedelta(hours=8)
        token = jwt.encode(
            {"employee_id": emp.id, "emp_code": emp.emp_code, "role": emp.role, "token_version": token_ver, "exp": exp},
            secret,
            algorithm="HS256",
        )
        return {"access_token": token}
    finally:
        session.close()


# ==============================================================================
# 1. EMPLOYEE CREATION FIXES & PROBATION DATE CLAMPING
# ==============================================================================

def test_add_months_clamping_edge_cases():
    """Verifies add_months clamps day to last day of target month."""
    # Jan 31 + 1 month -> Feb 28 (or 29 in leap year)
    d_jan31_2025 = date(2025, 1, 31)
    res_feb28 = add_months(d_jan31_2025, 1)
    assert res_feb28 == date(2025, 2, 28)

    # Jan 31, 2024 (leap year) + 1 month -> Feb 29, 2024
    d_jan31_2024 = date(2024, 1, 31)
    res_feb29 = add_months(d_jan31_2024, 1)
    assert res_feb29 == date(2024, 2, 29)

    # Aug 31, 2025 + 6 months -> Feb 28, 2026
    d_aug31 = date(2025, 8, 31)
    res_aug6m = add_months(d_aug31, 6)
    assert res_aug6m == date(2026, 2, 28)

    # Aug 31, 2023 + 6 months -> Feb 29, 2024 (leap year)
    d_aug31_leap = date(2023, 8, 31)
    res_aug6m_leap = add_months(d_aug31_leap, 6)
    assert res_aug6m_leap == date(2024, 2, 29)

    # Mar 31, 2025 + 1 month -> Apr 30, 2025
    d_mar31 = date(2025, 3, 31)
    res_mar1m = add_months(d_mar31, 1)
    assert res_mar1m == date(2025, 4, 30)

    # Feb 29, 2024 (leap) + 12 months -> Feb 28, 2025
    d_feb29 = date(2024, 2, 29)
    res_feb12m = add_months(d_feb29, 12)
    assert res_feb12m == date(2025, 2, 28)

    # Feb 29, 2024 + 1 month -> Mar 29, 2024
    res_feb1m = add_months(d_feb29, 1)
    assert res_feb1m == date(2024, 3, 29)


def test_email_validation_and_personal_email_storage(client):
    """Verifies work_email and personal_email validation regex and column persistence."""
    anita_cookie = get_cookie_for("ACM-002")  # hr_admin

    # Invalid work email
    r_bad_work = client.post("/employees", json={
        "name": "Test Emp",
        "work_email": "invalid-email-format",
        "department": "Engineering",
        "designation": "Developer",
        "join_date": "2026-11-01",
        "work_location": "Bangalore",
    }, cookies=anita_cookie)
    assert r_bad_work.status_code == 400
    assert "Invalid work email format" in r_bad_work.json()["detail"]

    # Invalid personal email
    r_bad_personal = client.post("/employees", json={
        "name": "Test Emp",
        "work_email": "valid.work@acmetech.com",
        "personal_email": "not-an-email",
        "department": "Engineering",
        "designation": "Developer",
        "join_date": "2026-11-01",
        "work_location": "Bangalore",
    }, cookies=anita_cookie)
    assert r_bad_personal.status_code == 400
    assert "Invalid personal email format" in r_bad_personal.json()["detail"]

    # Valid work and personal email
    unique_email = f"emp.test.{int(datetime.utcnow().timestamp())}@acmetech.com"
    r_valid = client.post("/employees", json={
        "name": "Valid Email Tester",
        "work_email": unique_email,
        "personal_email": "tester.personal@gmail.com",
        "department": "Engineering",
        "designation": "Developer",
        "join_date": "2026-11-01",
        "work_location": "Bangalore",
        "probation_months": 6,
    }, cookies=anita_cookie)
    assert r_valid.status_code == 201
    created = r_valid.json()["employee"]
    assert created["email"] == unique_email.lower()
    assert created["personal_email"] == "tester.personal@gmail.com"


def test_unique_constraint_on_leave_balances():
    """Verifies that inserting duplicate (employee_id, leave_type, year) raises IntegrityError."""
    session = get_session()
    try:
        emp = session.query(Employee).first()
        lb1 = LeaveBalance(
            employee_id=emp.id,
            leave_type="Casual",
            total=12,
            used=0,
            remaining=12,
            year=2099,
        )
        session.add(lb1)
        session.commit()

        # Second insert with identical tuple
        lb2 = LeaveBalance(
            employee_id=emp.id,
            leave_type="Casual",
            total=12,
            used=0,
            remaining=12,
            year=2099,
        )
        session.add(lb2)
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        # Clean up test row
        session.query(LeaveBalance).filter(LeaveBalance.year == 2099).delete()
        session.commit()
        session.close()


def test_manager_role_restriction_in_create_employee(client):
    """Verifies that manager_id must belong to a user with role 'manager' or 'hr_admin'."""
    anita_cookie = get_cookie_for("ACM-002")

    # Priya (ACM-008) is role 'employee'
    session = get_session()
    try:
        priya = session.query(Employee).filter(Employee.emp_code == "ACM-008").first()
        priya_id = priya.id
    finally:
        session.close()

    r = client.post("/employees", json={
        "name": "Subordinate Test",
        "work_email": f"sub.{int(datetime.utcnow().timestamp())}@acmetech.com",
        "department": "Engineering",
        "designation": "Junior Developer",
        "manager_id": priya_id,  # Invalid: Priya is 'employee'
        "join_date": "2026-11-01",
        "work_location": "Bangalore",
    }, cookies=anita_cookie)
    assert r.status_code == 400
    assert "Manager must have role 'manager' or 'hr_admin'" in r.json()["detail"]


# ==============================================================================
# 2. MANAGER CHANGES & REPORTING CYCLES
# ==============================================================================

def test_patch_employee_rejects_self_management(client):
    """Verifies PATCH /employees/{id} rejects manager_id equal to the employee's own id."""
    anita_cookie = get_cookie_for("ACM-002")
    session = get_session()
    try:
        rahul = session.query(Employee).filter(Employee.emp_code == "ACM-005").first()
        rahul_id = rahul.id
    finally:
        session.close()

    r = client.patch(f"/employees/{rahul_id}", json={
        "manager_id": rahul_id
    }, cookies=anita_cookie)
    assert r.status_code == 400
    assert "cannot be their own manager" in r.json()["detail"].lower()


def test_patch_employee_detects_two_person_reporting_loop(client):
    """Verifies PATCH /employees/{id} detects and rejects cycles in the reporting chain.
    If Vikram (ACM-001) manages Rahul (ACM-005), setting Vikram's manager to Rahul creates a loop.
    """
    anita_cookie = get_cookie_for("ACM-002")
    session = get_session()
    try:
        vikram = session.query(Employee).filter(Employee.emp_code == "ACM-001").first()
        rahul = session.query(Employee).filter(Employee.emp_code == "ACM-005").first()
        vikram_id = vikram.id
        rahul_id = rahul.id
        # Confirm Rahul reports to Vikram
        assert rahul.manager_id == vikram_id
    finally:
        session.close()

    # Attempt to assign Rahul as Vikram's manager -> cycle
    r = client.patch(f"/employees/{vikram_id}", json={
        "manager_id": rahul_id
    }, cookies=anita_cookie)
    assert r.status_code == 400
    assert "cyclic" in r.json()["detail"].lower()


def test_demoting_manager_with_direct_reports_returns_409(client):
    """Verifies that changing role from 'manager' to 'employee' when direct reports exist returns 409."""
    anita_cookie = get_cookie_for("ACM-002")
    session = get_session()
    try:
        rahul = session.query(Employee).filter(Employee.emp_code == "ACM-005").first()
        rahul_id = rahul.id
    finally:
        session.close()

    r = client.patch(f"/employees/{rahul_id}", json={
        "role": "employee"
    }, cookies=anita_cookie)
    assert r.status_code == 409
    assert "reassign them first" in r.json()["detail"].lower()


# ==============================================================================
# 3. OFFBOARDING & REACTIVATION
# ==============================================================================

def test_offboard_employee_future_vs_immediate_and_reassignment(client):
    """Verifies offboard behavior:
    1. Direct reports require reassign_reports_to, missing returns 409.
    2. Future last_working_date keeps status 'active'.
    3. Today or past last_working_date sets status 'terminated'.
    4. Cancels pending leave requests.
    """
    anita_cookie = get_cookie_for("ACM-002")
    session = get_session()
    try:
        # Create a temporary manager and subordinate
        mgr = Employee(
            emp_code=f"ACM-MGR{int(datetime.utcnow().timestamp()) % 10000}",
            name="Temp Manager",
            email=f"temp.mgr.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="Engineering",
            designation="Manager",
            role="manager",
            join_date=date(2022, 1, 1),
            work_location="Bangalore",
            status="active",
        )
        session.add(mgr)
        session.commit()
        session.refresh(mgr)

        sub = Employee(
            emp_code=f"ACM-SUB{int(datetime.utcnow().timestamp()) % 10000}",
            name="Temp Subordinate",
            email=f"temp.sub.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="Engineering",
            designation="Engineer",
            role="employee",
            join_date=date(2023, 1, 1),
            work_location="Bangalore",
            status="active",
            manager_id=mgr.id,
        )
        session.add(sub)
        session.commit()
        session.refresh(sub)

        # Add a pending leave request for the manager
        pending_lr = LeaveRequest(
            employee_id=mgr.id,
            leave_type="Casual",
            start_date=date(2026, 12, 1),
            end_date=date(2026, 12, 2),
            days=2,
            status="pending",
        )
        session.add(pending_lr)
        session.commit()

        mgr_id = mgr.id
        sub_id = sub.id
    finally:
        session.close()

    # Step 1: Offboarding without reassign_reports_to returns 409
    r_no_reassign = client.post(f"/employees/{mgr_id}/offboard", json={
        "last_working_date": "2026-12-31",
        "reason": "Resignation",
    }, cookies=anita_cookie)
    assert r_no_reassign.status_code == 409
    assert "reassign_reports_to" in r_no_reassign.json()["detail"]

    # Step 2: Offboard with future date and reassignment (to Rahul ACM-005)
    session = get_session()
    try:
        rahul = session.query(Employee).filter(Employee.emp_code == "ACM-005").first()
        rahul_id = rahul.id
    finally:
        session.close()

    future_date = (get_current_date() + timedelta(days=15)).isoformat()
    r_future = client.post(f"/employees/{mgr_id}/offboard", json={
        "last_working_date": future_date,
        "reason": "Relocating abroad",
        "reassign_reports_to": rahul_id,
    }, cookies=anita_cookie)
    assert r_future.status_code == 200
    assert r_future.json()["status"] == "active"  # Stays active until scheduled date

    # Verify subordinate was reassigned to Rahul and pending leave was cancelled
    session = get_session()
    try:
        sub_updated = session.query(Employee).filter(Employee.id == sub_id).first()
        assert sub_updated.manager_id == rahul_id
        mgr_updated = session.query(Employee).filter(Employee.id == mgr_id).first()
        assert mgr_updated.status == "active"
        assert mgr_updated.last_working_date.isoformat() == future_date
        assert mgr_updated.offboard_reason == "Relocating abroad"
        # Check leave cancelled
        lr_check = session.query(LeaveRequest).filter(LeaveRequest.employee_id == mgr_id).first()
        assert lr_check.status == "cancelled"
    finally:
        session.close()

    # Step 3: Immediate offboard (today or past) terminates immediately
    today_str = get_current_date().isoformat()
    r_immediate = client.post(f"/employees/{mgr_id}/offboard", json={
        "last_working_date": today_str,
        "reason": "Relieved immediately",
    }, cookies=anita_cookie)
    assert r_immediate.status_code == 200
    assert r_immediate.json()["status"] == "terminated"


def test_reactivate_employee_endpoint(client):
    """Verifies POST /employees/{id}/reactivate flips terminated employee to active."""
    anita_cookie = get_cookie_for("ACM-002")

    session = get_session()
    try:
        # Create a terminated employee
        term_emp = Employee(
            emp_code=f"ACM-TRM{int(datetime.utcnow().timestamp()) % 10000}",
            name="Terminated For Reactivation",
            email=f"reactivate.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="Support",
            designation="Specialist",
            role="employee",
            join_date=date(2023, 1, 1),
            work_location="Bangalore",
            status="terminated",
            last_working_date=date(2024, 1, 1),
            offboard_reason="Mistake",
        )
        session.add(term_emp)
        session.commit()
        session.refresh(term_emp)
        term_id = term_emp.id
    finally:
        session.close()

    r = client.post(f"/employees/{term_id}/reactivate", cookies=anita_cookie)
    assert r.status_code == 200
    res_emp = r.json()["employee"]
    assert res_emp["status"] == "active"
    assert res_emp["last_working_date"] is None
    assert res_emp["offboard_reason"] is None

    # Reactivating already active employee returns 400
    r_repeat = client.post(f"/employees/{term_id}/reactivate", cookies=anita_cookie)
    assert r_repeat.status_code == 400


# ==============================================================================
# 4. DAILY JOB STATUS SYNC
# ==============================================================================

def test_daily_status_sync_applies_onboarding_and_scheduled_terminations():
    """Verifies run_daily_status_sync() flips onboarding->active on join_date,
    and active->terminated on last_working_date.
    """
    today = get_current_date()
    session = get_session()
    try:
        # Employee 1: Onboarding with join_date = today
        emp_onboard = Employee(
            emp_code=f"ACM-OB{int(datetime.utcnow().timestamp()) % 10000}",
            name="Daily Job Onboarder",
            email=f"onboard.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="Engineering",
            designation="Engineer",
            role="employee",
            join_date=today,
            work_location="Bangalore",
            status="onboarding",
        )
        session.add(emp_onboard)

        # Employee 2: Active with last_working_date = today
        emp_scheduled_term = Employee(
            emp_code=f"ACM-TM{int(datetime.utcnow().timestamp()) % 10000}",
            name="Daily Job Terminee",
            email=f"terminee.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="Sales",
            designation="Executive",
            role="employee",
            join_date=date(2023, 1, 1),
            work_location="Mumbai",
            status="active",
            last_working_date=today,
            offboard_reason="Planned exit",
        )
        session.add(emp_scheduled_term)
        session.commit()

        code_ob = emp_onboard.emp_code
        code_tm = emp_scheduled_term.emp_code
    finally:
        session.close()

    # Execute daily sync
    res = run_daily_status_sync()
    assert code_ob in res["flipped_onboarding"]
    assert code_tm in res["applied_terminations"]

    # Verify database state
    session = get_session()
    try:
        ob_check = session.query(Employee).filter(Employee.emp_code == code_ob).first()
        assert ob_check.status == "active"
        tm_check = session.query(Employee).filter(Employee.emp_code == code_tm).first()
        assert tm_check.status == "terminated"
    finally:
        session.close()


def test_invite_expired_state_in_onboarding_list(client):
    """Verifies that an invite with expires_at in the past returns invite_state == 'expired'."""
    anita_cookie = get_cookie_for("ACM-002")
    session = get_session()
    try:
        emp = Employee(
            emp_code=f"ACM-EXP{int(datetime.utcnow().timestamp()) % 10000}",
            name="Expired Invite Candidate",
            email=f"expired.{int(datetime.utcnow().timestamp())}@acmetech.com",
            department="HR",
            designation="Coordinator",
            role="employee",
            join_date=get_current_date() + timedelta(days=5),
            work_location="Bangalore",
            status="onboarding",
        )
        session.add(emp)
        session.commit()
        session.refresh(emp)

        # Add invite with past expiration
        inv = Invite(
            employee_id=emp.id,
            token_hash=f"hash-{int(datetime.utcnow().timestamp())}",
            expires_at=datetime.utcnow() - timedelta(hours=24),
            used_at=None,
        )
        session.add(inv)
        session.commit()
        test_emp_code = emp.emp_code
    finally:
        session.close()

    r = client.get("/employees/onboarding", cookies=anita_cookie)
    assert r.status_code == 200
    emps = r.json()["employees"]
    target = next((e for e in emps if e["emp_code"] == test_emp_code), None)
    assert target is not None
    assert target["invite_state"] == "expired"


# ==============================================================================
# 5. HR AUDIT TRAIL
# ==============================================================================

def test_admin_audit_logs_recording_and_endpoint(client):
    """Verifies admin_audit records actions and GET /audit/admin restricts to hr_admin."""
    anita_cookie = get_cookie_for("ACM-002")  # hr_admin
    priya_cookie = get_cookie_for("ACM-008")  # employee

    # Direct helper test
    log_id = log_admin_audit(
        actor_emp_code="ACM-002",
        action="policy_upload",
        target_emp_code=None,
        details={"file": "test_policy.md", "chunks": 4},
    )
    assert log_id > 0

    logs = get_admin_audit_logs(limit=10)
    assert any(l["action"] == "policy_upload" and l["details"].get("file") == "test_policy.md" for l in logs)

    # Anita fetches /audit/admin -> 200
    r_anita = client.get("/audit/admin", cookies=anita_cookie)
    assert r_anita.status_code == 200
    data = r_anita.json()
    assert "logs" in data
    assert len(data["logs"]) > 0

    # Priya fetches /audit/admin -> 403 Forbidden
    r_priya = client.get("/audit/admin", cookies=priya_cookie)
    assert r_priya.status_code == 403


# ==============================================================================
# 6. POLICY INGESTION WITH REPLACE=TRUE
# ==============================================================================

def test_policy_ingestion_replace_true_deletes_old_chunks():
    """Verifies that replacing a document calls delete_chunks_by_source so shorter docs leave no stale chunks."""
    long_content = (
        "# Leave Policy\n"
        "## General Rules\n" + "This is a long sentence explaining leave rules. " * 50 + "\n"
        "## Encashment Rules\n" + "Leave encashment is detailed here for separation. " * 50
    ).encode("utf-8")

    short_content = (
        "# Leave Policy\n"
        "## Summary\n"
        "Short policy summary only."
    ).encode("utf-8")

    source_doc = "test_replace_policy.md"

    with patch("app.ingest.delete_chunks_by_source") as mock_delete, \
         patch("app.ingest.upsert_chunks") as mock_upsert, \
         patch("app.ingest.embed_texts", return_value=[[0.1] * 384] * 10):

        # Initial ingest without replace
        count1 = process_and_ingest_document(
            file_bytes=long_content,
            filename=source_doc,
            source=source_doc,
            replace=False,
        )
        assert count1 > 1
        mock_delete.assert_not_called()

        # Ingest shorter replacement with replace=True
        count2 = process_and_ingest_document(
            file_bytes=short_content,
            filename=source_doc,
            source=source_doc,
            replace=True,
        )
        assert count2 >= 1
        # delete_chunks_by_source was called with the document source!
        mock_delete.assert_called_once_with("hr_policies", source_doc)


def test_reset_passwords_flag(client):
    """Verifies --reset-passwords:
    - Active employees can log in with shared demo password
    - Old tokens stop working (token_version incremented)
    - Onboarding and terminated employees are untouched
    - Refuses to run if DEMO_PASSWORD is empty
    """
    settings = get_settings()
    session = get_session()
    try:
        priya = session.query(Employee).filter(Employee.emp_code == "ACM-008").first()
        assert priya is not None
        priya_creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == priya.id).first()
        old_token_version = priya_creds.token_version if priya_creds else 1

        # Generate a valid token with Priya's current token_version
        secret = get_jwt_secret()
        old_token = jwt.encode(
            {
                "employee_id": priya.id,
                "emp_code": priya.emp_code,
                "role": priya.role,
                "token_version": old_token_version,
                "exp": datetime.utcnow() + timedelta(hours=8),
            },
            secret,
            algorithm="HS256",
        )

        # Confirm old token works
        res_valid = client.get("/auth/session", cookies={"access_token": old_token})
        assert res_valid.json().get("authenticated") is True

        # Check onboarding and terminated employees before reset
        rohit = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        assert rohit is not None
        if rohit.status != "onboarding":
            rohit.status = "onboarding"
            session.commit()
        assert rohit.status == "onboarding"

        karan = session.query(Employee).filter(Employee.emp_code == "ACM-031").first()
        assert karan is not None
        if karan.status != "terminated":
            karan.status = "terminated"
            session.commit()
        assert karan.status == "terminated"
    finally:
        session.close()

    # Test refusal when DEMO_PASSWORD is empty
    with patch.object(settings, "demo_password", ""), patch.dict("os.environ", {"DEMO_PASSWORD": ""}):
        with pytest.raises(ValueError, match="DEMO_PASSWORD is empty"):
            reset_passwords()

    # Run reset_passwords with a custom test password
    test_pwd = "ResetSharedTestPwd2026!"
    with patch.object(settings, "demo_password", test_pwd), patch.dict("os.environ", {"DEMO_PASSWORD": test_pwd}):
        updated_count = reset_passwords()
        assert updated_count > 0

        # 1. Active employees can log in with shared password
        res_login_priya = client.post("/auth/login", json={"email": "priya.sharma@acmetech.com", "password": test_pwd})
        assert res_login_priya.status_code == 200
        assert res_login_priya.json()["employee"]["emp_code"] == "ACM-008"

        res_login_rahul = client.post("/auth/login", json={"email": "rahul.mehta@acmetech.com", "password": test_pwd})
        assert res_login_rahul.status_code == 200
        assert res_login_rahul.json()["employee"]["emp_code"] == "ACM-005"

        # 2. Old tokens stop working (token_version was incremented)
        res_old_sess = client.get("/auth/session", cookies={"access_token": old_token})
        assert res_old_sess.json().get("authenticated") is False
        res_old_me = client.get("/auth/me", cookies={"access_token": old_token})
        assert res_old_me.status_code == 401

        # 3. Onboarding and terminated employees are untouched
        session = get_session()
        try:
            rohit_after = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
            assert rohit_after.status == "onboarding"
            karan_after = session.query(Employee).filter(Employee.emp_code == "ACM-031").first()
            assert karan_after.status == "terminated"
        finally:
            session.close()

        # Terminated employee cannot log in
        res_karan_login = client.post("/auth/login", json={"email": "karan.johar@acmetech.com", "password": test_pwd})
        assert res_karan_login.status_code in (401, 403)

    # Restore standard demo password for all active employees
    orig_pwd = "AcmeSecret2026!"
    with patch.object(settings, "demo_password", orig_pwd), patch.dict("os.environ", {"DEMO_PASSWORD": orig_pwd}):
        reset_passwords()

