"""Employee management routes: create, update, offboard, reactivate, and invite employees.

All routes are hr_admin only except those that serve general lookup needs.
"""
import hashlib
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.auth import (
    CallerContext,
    clear_caller_cache,
    get_current_caller,
    hash_password,
    invalidate_auth_cache,
)
from app.db import (
    AuthCredentials,
    Employee,
    Invite,
    LeaveBalance,
    LeaveRequest,
    get_session,
    log_admin_audit,
)
from app.utils import add_months, get_current_date

logger = logging.getLogger("hrms-bot.employee_routes")

employee_router = APIRouter(prefix="/employees", tags=["Employees"])

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

# Base annual leave entitlements (days)
LEAVE_ENTITLEMENTS = {
    "Casual": 12,
    "Sick": 10,
    "Earned": 18,
}


def _compute_prorated_leaves(join_date: date, year: int) -> Dict[str, int]:
    """Compute prorated leave days for the joining year based on months remaining."""
    months_remaining = 12 - join_date.month + 1  # inclusive of join month
    months_remaining = max(0, min(12, months_remaining))

    prorated = {}
    for leave_type, annual_days in LEAVE_ENTITLEMENTS.items():
        prorated_days = max(1, round(annual_days * months_remaining / 12))
        prorated[leave_type] = prorated_days
    return prorated


def _generate_next_emp_code(session) -> str:
    """Generate the next sequential ACM-0XX employee code."""
    employees = session.query(Employee.emp_code).all()
    max_num = 0
    for (code,) in employees:
        if code and code.upper().startswith("ACM-"):
            try:
                num = int(code[4:])
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
    next_num = max_num + 1
    return f"ACM-{next_num:03d}"


# ---- Pydantic models ----

class CreateEmployeeRequest(BaseModel):
    name: str
    personal_email: Optional[str] = None
    work_email: str
    department: str
    designation: str
    manager_id: Optional[int] = None
    role: str = "employee"
    join_date: str  # ISO date string YYYY-MM-DD
    work_location: str
    probation_months: int = 6  # months of probation, default 6


class UpdateEmployeeRequest(BaseModel):
    department: Optional[str] = None
    designation: Optional[str] = None
    manager_id: Optional[int] = None
    role: Optional[str] = None
    status: Optional[str] = None
    work_location: Optional[str] = None


class OffboardRequest(BaseModel):
    last_working_date: str  # ISO date string YYYY-MM-DD
    reason: Optional[str] = None
    reassign_reports_to: Optional[int] = None


# ---- Helper: employee to dict ----

def _emp_to_dict(emp: Employee, session) -> Dict[str, Any]:
    mgr = session.query(Employee).filter(Employee.id == emp.manager_id).first() if emp.manager_id else None

    # Check invite state
    invites = sorted(emp.invites, key=lambda x: x.id, reverse=True) if hasattr(emp, "invites") else []
    invite_state = "not_sent"
    if invites:
        latest = invites[0]
        if latest.used_at:
            invite_state = "activated"
        elif latest.expires_at < datetime.utcnow():
            invite_state = "expired"
        else:
            invite_state = "sent"

    return {
        "id": emp.id,
        "emp_code": emp.emp_code,
        "name": emp.name,
        "email": emp.email,
        "personal_email": emp.personal_email,
        "department": emp.department,
        "designation": emp.designation,
        "manager_id": emp.manager_id,
        "manager_name": mgr.name if mgr else None,
        "role": emp.role,
        "join_date": emp.join_date.isoformat(),
        "work_location": emp.work_location,
        "status": emp.status,
        "probation_end_date": emp.probation_end_date.isoformat() if emp.probation_end_date else None,
        "last_working_date": emp.last_working_date.isoformat() if emp.last_working_date else None,
        "offboard_reason": emp.offboard_reason,
        "invite_state": invite_state,
    }


# ---- Routes ----

@employee_router.post("", status_code=201)
async def create_employee(
    req: CreateEmployeeRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    """Create a new employee. HR Admin only.

    - Validates work_email and personal_email.
    - Ensures manager_id has role 'manager' or 'hr_admin'.
    - Safe sequential emp_code with retries on collision.
    - Clamps probation_end_date with add_months().
    - Creates prorated leave balances and auth credentials.
    """
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    # Validate emails
    if not EMAIL_REGEX.match(req.work_email.strip()):
        raise HTTPException(status_code=400, detail="Invalid work email format.")
    if req.personal_email and not EMAIL_REGEX.match(req.personal_email.strip()):
        raise HTTPException(status_code=400, detail="Invalid personal email format.")

    # Parse and validate join_date
    try:
        join_date = date.fromisoformat(req.join_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid join_date. Use YYYY-MM-DD format.")

    today = get_current_date()
    if join_date < today - timedelta(days=365):
        raise HTTPException(status_code=400, detail="join_date cannot be more than 1 year in the past.")
    if join_date > today + timedelta(days=730):
        raise HTTPException(status_code=400, detail="join_date cannot be more than 2 years in the future.")

    # Validate role
    valid_roles = {"employee", "manager", "hr_admin"}
    if req.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")

    session = get_session()
    try:
        # Check unique work_email
        existing = session.query(Employee).filter(
            func.lower(Employee.email) == req.work_email.strip().lower()
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"An employee with email '{req.work_email}' already exists.")

        # Validate manager_id if provided (must be manager or hr_admin)
        if req.manager_id is not None:
            mgr = session.query(Employee).filter(Employee.id == req.manager_id).first()
            if not mgr:
                raise HTTPException(status_code=400, detail=f"Manager with id {req.manager_id} not found.")
            if mgr.role not in ("manager", "hr_admin"):
                raise HTTPException(status_code=400, detail=f"Manager must have role 'manager' or 'hr_admin' (found '{mgr.role}').")

        status = "onboarding" if join_date >= today else "active"

        # Compute probation end date with day clamping
        probation_months = max(1, min(24, req.probation_months))
        probation_end_date = add_months(join_date, probation_months)

        # Retry employee code generation up to 3 times on unique constraint collision
        created_emp = None
        for attempt in range(3):
            emp_code = _generate_next_emp_code(session)
            try:
                emp = Employee(
                    emp_code=emp_code,
                    name=req.name.strip(),
                    email=req.work_email.strip().lower(),
                    personal_email=req.personal_email.strip().lower() if req.personal_email else None,
                    department=req.department.strip(),
                    designation=req.designation.strip(),
                    manager_id=req.manager_id,
                    role=req.role,
                    join_date=join_date,
                    work_location=req.work_location.strip(),
                    status=status,
                    probation_end_date=probation_end_date,
                )
                session.add(emp)
                session.flush()  # get emp.id

                # Create prorated leave balances for join year
                prorated = _compute_prorated_leaves(join_date, join_date.year)
                for leave_type, days in prorated.items():
                    lb = LeaveBalance(
                        employee_id=emp.id,
                        leave_type=leave_type,
                        total=days,
                        used=0,
                        remaining=days,
                        year=join_date.year,
                    )
                    session.add(lb)

                # Create auth_credentials with must_change_password=True
                creds = AuthCredentials(
                    employee_id=emp.id,
                    password_hash=hash_password(secrets.token_urlsafe(16)),
                    must_change_password=True,
                    failed_attempts=0,
                    token_version=1,
                )
                session.add(creds)
                session.commit()
                created_emp = emp
                break
            except IntegrityError as ie:
                session.rollback()
                err_str = str(ie).lower()
                if "email" in err_str:
                    raise HTTPException(status_code=409, detail=f"An employee with email '{req.work_email}' already exists.")
                if attempt == 2:
                    raise HTTPException(status_code=500, detail="Failed to allocate unique employee code after 3 attempts.")

        # Log admin audit
        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="create_employee",
            target_emp_code=created_emp.emp_code,
            details={
                "name": created_emp.name,
                "email": created_emp.email,
                "personal_email": created_emp.personal_email,
                "department": created_emp.department,
                "role": created_emp.role,
                "manager_id": created_emp.manager_id,
                "join_date": created_emp.join_date.isoformat(),
                "probation_end_date": probation_end_date.isoformat(),
            },
        )

        logger.info(f"HR Admin {caller.emp_code} created employee {created_emp.emp_code} ({created_emp.name})")

        return {
            "message": f"Employee {created_emp.name} created successfully.",
            "employee": _emp_to_dict(created_emp, session),
            "leave_balances": [
                {"leave_type": lt, "total": days, "remaining": days, "year": join_date.year}
                for lt, days in prorated.items()
            ],
        }
    finally:
        session.close()


@employee_router.patch("/{employee_id}")
async def update_employee(
    employee_id: int,
    req: UpdateEmployeeRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    """Update employee details. HR Admin only.

    - Rejects self-management (manager_id == id).
    - Detects and rejects reporting cycles.
    - Rejects changing role from 'manager' to 'employee' when direct reports exist.
    - Records admin audit trail.
    """
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=f"Employee with id {employee_id} not found.")

        changed_sensitive = False
        changed_fields = {}

        if req.department is not None:
            emp.department = req.department.strip()
            changed_fields["department"] = emp.department
        if req.designation is not None:
            emp.designation = req.designation.strip()
            changed_fields["designation"] = emp.designation

        if req.manager_id is not None:
            # Self-management check
            if req.manager_id == emp.id:
                raise HTTPException(status_code=400, detail="An employee cannot be their own manager.")

            mgr = session.query(Employee).filter(Employee.id == req.manager_id).first()
            if not mgr:
                raise HTTPException(status_code=400, detail=f"Manager with id {req.manager_id} not found.")
            if mgr.role not in ("manager", "hr_admin"):
                raise HTTPException(status_code=400, detail=f"Assigned manager must have role 'manager' or 'hr_admin' (found '{mgr.role}').")

            # Cycle detection in reporting chain
            curr = mgr
            visited = set()
            while curr:
                if curr.id == emp.id:
                    raise HTTPException(status_code=400, detail="Cannot assign manager: creates a cyclic reporting relationship.")
                if curr.id in visited:
                    break
                visited.add(curr.id)
                curr = session.query(Employee).filter(Employee.id == curr.manager_id).first() if curr.manager_id else None

            changed_fields["manager_id"] = req.manager_id
            emp.manager_id = req.manager_id

        if req.work_location is not None:
            emp.work_location = req.work_location.strip()
            changed_fields["work_location"] = emp.work_location

        if req.role is not None:
            valid_roles = {"employee", "manager", "hr_admin"}
            if req.role not in valid_roles:
                raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")

            # Check demoting manager to employee when direct reports exist
            if req.role == "employee" and emp.role == "manager":
                direct_reports_count = session.query(Employee).filter(
                    Employee.manager_id == emp.id,
                    Employee.status != "terminated",
                ).count()
                if direct_reports_count > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot change role to 'employee': {emp.name} currently manages {direct_reports_count} direct report(s). Please reassign them first.",
                    )

            if emp.role != req.role:
                changed_fields["role"] = {"old": emp.role, "new": req.role}
                emp.role = req.role
                changed_sensitive = True

        if req.status is not None:
            valid_statuses = {"active", "onboarding", "terminated"}
            if req.status not in valid_statuses:
                raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")
            if emp.status != req.status:
                changed_fields["status"] = {"old": emp.status, "new": req.status}
                emp.status = req.status
                changed_sensitive = True

        session.commit()

        # Clear caller cache on role/status change
        if changed_sensitive:
            clear_caller_cache()
            invalidate_auth_cache(emp.id)
            logger.info(f"Caller cache cleared after role/status change for employee {emp.emp_code}")

        # Log admin audit
        action_name = "role_change" if "role" in changed_fields else "update_employee"
        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action=action_name,
            target_emp_code=emp.emp_code,
            details={"changed_fields": changed_fields},
        )

        logger.info(f"HR Admin {caller.emp_code} updated employee {emp.emp_code}")
        return {"message": f"Employee {emp.name} updated.", "employee": _emp_to_dict(emp, session)}
    finally:
        session.close()


@employee_router.post("/{employee_id}/offboard")
async def offboard_employee(
    employee_id: int,
    req: OffboardRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    """Offboard an employee: stores last_working_date, offboard_reason, cancels pending leaves, and schedules or executes termination."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    try:
        last_working_date = date.fromisoformat(req.last_working_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid last_working_date. Use YYYY-MM-DD format.")

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=f"Employee with id {employee_id} not found.")

        if emp.status == "terminated":
            raise HTTPException(status_code=400, detail="Employee is already terminated.")

        # Reassign direct reports if any
        direct_reports = session.query(Employee).filter(
            Employee.manager_id == emp.id,
            Employee.status != "terminated",
        ).all()
        if direct_reports:
            if not req.reassign_reports_to:
                raise HTTPException(
                    status_code=409,
                    detail=f"Employee {emp.name} currently has {len(direct_reports)} direct report(s). Please provide 'reassign_reports_to' manager id to reassign them.",
                )
            new_mgr = session.query(Employee).filter(Employee.id == req.reassign_reports_to).first()
            if not new_mgr:
                raise HTTPException(status_code=400, detail=f"Reassignment manager with id {req.reassign_reports_to} not found.")
            if new_mgr.id == emp.id:
                raise HTTPException(status_code=400, detail="Cannot reassign reports to the employee being offboarded.")
            if new_mgr.role not in ("manager", "hr_admin"):
                raise HTTPException(status_code=400, detail=f"Reassignment manager must have role 'manager' or 'hr_admin' (found '{new_mgr.role}').")

            for r in direct_reports:
                r.manager_id = new_mgr.id

        # Cancel leaver's pending leave requests
        pending_leaves = session.query(LeaveRequest).filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.status == "pending",
        ).all()
        for pl in pending_leaves:
            pl.status = "cancelled"

        today = get_current_date()
        emp.last_working_date = last_working_date
        emp.offboard_reason = req.reason.strip() if req.reason else None

        if last_working_date > today:
            # Future termination: remain active until scheduled date
            emp.status = "active"
            new_status = "active"
        else:
            # Today or past: terminate immediately
            emp.status = "terminated"
            new_status = "terminated"
            clear_caller_cache()
            invalidate_auth_cache(emp.id)

        session.commit()

        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="offboard_employee",
            target_emp_code=emp.emp_code,
            details={
                "last_working_date": last_working_date.isoformat(),
                "reason": req.reason,
                "status": new_status,
                "reassigned_direct_reports": len(direct_reports) if direct_reports else 0,
                "cancelled_leaves": len(pending_leaves),
            },
        )

        logger.info(
            f"HR Admin {caller.emp_code} offboarded employee {emp.emp_code} ({emp.name}). "
            f"Last working date: {last_working_date}. Status: {new_status}. Reason: {req.reason or 'Not provided'}"
        )

        return {
            "message": f"Employee {emp.name} ({emp.emp_code}) offboarding processed successfully.",
            "last_working_date": last_working_date.isoformat(),
            "status": new_status,
        }
    finally:
        session.close()


@employee_router.post("/{employee_id}/reactivate")
async def reactivate_employee(
    employee_id: int,
    caller: CallerContext = Depends(get_current_caller),
):
    """Reactivate a previously terminated employee. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=f"Employee with id {employee_id} not found.")

        if emp.status != "terminated":
            raise HTTPException(status_code=400, detail=f"Employee is not terminated (current status: '{emp.status}').")

        emp.status = "active"
        emp.last_working_date = None
        emp.offboard_reason = None
        session.commit()
        clear_caller_cache()
        invalidate_auth_cache(emp.id)

        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="reactivate_employee",
            target_emp_code=emp.emp_code,
            details={"message": "Reactivated by HR Admin"},
        )

        logger.info(f"HR Admin {caller.emp_code} reactivated employee {emp.emp_code} ({emp.name})")

        return {
            "message": f"Employee {emp.name} ({emp.emp_code}) has been reactivated successfully.",
            "employee": _emp_to_dict(emp, session),
        }
    finally:
        session.close()


@employee_router.post("/{employee_id}/invite")
async def generate_invite(
    employee_id: int,
    caller: CallerContext = Depends(get_current_caller),
):
    """Generate a single-use activation invite link for a new employee. HR Admin only.

    Returns the activation link with a token. A stub send_email() is called but is a no-op.
    """
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=f"Employee with id {employee_id} not found.")

        # Revoke older unused invites when a new one is created
        session.query(Invite).filter(
            Invite.employee_id == emp.id,
            Invite.used_at.is_(None),
        ).delete()

        # If active employee, set must_change_password=True and revoke token_version
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        if creds and emp.status == "active":
            creds.must_change_password = True
            creds.token_version += 1
            clear_caller_cache()
            invalidate_auth_cache(emp.id)

        # Generate a secure token
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # 72-hour expiry
        expires_at = datetime.utcnow() + timedelta(hours=72)

        invite = Invite(
            employee_id=emp.id,
            token_hash=token_hash,
            expires_at=expires_at,
            used_at=None,
        )
        session.add(invite)
        session.commit()

        activation_link = f"/activate?token={raw_token}"

        _send_invite_email_stub(
            to_email=emp.email,
            employee_name=emp.name,
            emp_code=emp.emp_code,
            expires_at=expires_at,
        )

        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="generate_invite",
            target_emp_code=emp.emp_code,
            details={"expires_at": expires_at.isoformat()},
        )

        logger.info(
            f"HR Admin {caller.emp_code} generated invite for {emp.emp_code} ({emp.name}). "
            f"Expires: {expires_at.isoformat()}"
        )

        return {
            "message": f"Invite generated for {emp.name}.",
            "employee_id": emp.id,
            "emp_code": emp.emp_code,
            "activation_link": activation_link,
            "token": raw_token,
            "expires_at": expires_at.isoformat(),
        }
    finally:
        session.close()


def _send_invite_email_stub(to_email: str, employee_name: str, emp_code: str, expires_at: datetime) -> None:
    """STUB: Send activation email to employee. Does not log full token/link."""
    logger.info(
        f"[EMAIL STUB] Would send activation email to {to_email} ({employee_name}, {emp_code}). "
        f"Expires: {expires_at.isoformat()}"
    )


@employee_router.get("/onboarding", tags=["HR Admin"])
async def list_onboarding_employees(
    caller: CallerContext = Depends(get_current_caller),
):
    """List all employees in 'onboarding' status with their invite state. HR Admin only."""
    if caller.role != "hr_admin":
        raise HTTPException(status_code=403, detail="Forbidden: HR Admin access required.")

    session = get_session()
    try:
        employees = (
            session.query(Employee)
            .filter(Employee.status == "onboarding")
            .order_by(Employee.join_date.asc())
            .all()
        )
        return {
            "count": len(employees),
            "employees": [_emp_to_dict(emp, session) for emp in employees],
        }
    finally:
        session.close()
