import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import bcrypt
from fastapi import HTTPException, Request
import jwt
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import AuthCredentials, Employee, get_session

logger = logging.getLogger("hrms-bot.auth")


def hash_password(password: str) -> str:
    """Hashes a plaintext password using bcrypt. Never stores plaintext."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verifies a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


class CallerContext(BaseModel):
    """Context object representing the authenticated caller."""
    employee_id: int
    emp_code: str
    name: str
    email: str
    department: str
    designation: str
    role: str  # 'employee' | 'manager' | 'hr_admin'
    status: str = "active"  # 'active' | 'onboarding' | 'terminated'
    join_date: Optional[str] = None
    subordinate_ids: List[int] = []

    def can_access_employee(self, target_employee_id: int) -> bool:
        """Checks if the caller is authorized to view target employee's general HR data."""
        if self.role == "hr_admin":
            return True
        if self.employee_id == target_employee_id:
            return True
        if self.role == "manager" and target_employee_id in self.subordinate_ids:
            return True
        return False

    def can_access_payroll(self, target_employee_id: int) -> bool:
        """Checks if caller is authorized to view target employee's payroll/salary."""
        # Own records allowed for anyone
        if self.employee_id == target_employee_id:
            return True
        # For anyone else, ONLY hr_admin is allowed
        if self.role == "hr_admin":
            return True
        return False


def get_all_subordinate_ids(session: Session, manager_id: int) -> List[int]:
    """Recursively retrieves all direct and indirect report IDs for a manager."""
    subordinates: Set[int] = set()
    queue = [manager_id]

    while queue:
        current_m_id = queue.pop(0)
        direct_reports = (
            session.query(Employee.id)
            .filter(Employee.manager_id == current_m_id, Employee.id != current_m_id)
            .all()
        )
        for (sub_id,) in direct_reports:
            if sub_id not in subordinates:
                subordinates.add(sub_id)
                queue.append(sub_id)

    return sorted(list(subordinates))


# 60-second TTL caller cache: key -> (CallerContext, timestamp)
_CALLER_CACHE: Dict[str, Tuple[CallerContext, float]] = {}
CALLER_CACHE_TTL = 60.0

# 30-second TTL verified caller auth cache
# Key: employee_id -> (CallerContext, token_version, must_change_password, timestamp)
_AUTH_VERIFY_CACHE: Dict[int, Tuple[CallerContext, int, bool, float]] = {}
AUTH_CACHE_TTL = 30.0


def invalidate_auth_cache(employee_id: Optional[int] = None) -> None:
    """Invalidates the 30-second verified auth cache.

    Called on password change, role/status change, logout, and invite/reset.
    """
    if employee_id is not None:
        _AUTH_VERIFY_CACHE.pop(employee_id, None)
    else:
        _AUTH_VERIFY_CACHE.clear()
    clear_caller_cache()


def clear_caller_cache() -> None:
    """Clears the cached caller contexts. Called when role or status changes."""
    _CALLER_CACHE.clear()


def _get_from_cache(key: str) -> Optional[CallerContext]:
    entry = _CALLER_CACHE.get(key)
    if entry:
        ctx, ts = entry
        if time.time() - ts < CALLER_CACHE_TTL:
            return ctx
        else:
            del _CALLER_CACHE[key]
    return None


def _set_in_cache(key: str, ctx: CallerContext) -> None:
    _CALLER_CACHE[key] = (ctx, time.time())


def resolve_caller(identifier: str | int) -> CallerContext:
    """Resolves an employee ID, emp_code, email, or exact name into a full CallerContext.

    Uses exact case-insensitive matching (func.lower ==), never ilike wildcards.
    Caches results in a 60-second TTL cache.
    Raises ValueError if employee is not found.
    """
    cache_key = str(identifier).strip().lower()
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    session = get_session()
    try:
        emp = None
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
            emp = session.query(Employee).filter(Employee.id == int(identifier)).first()
        elif isinstance(identifier, str):
            clean_str = identifier.strip().lower()
            emp = (
                session.query(Employee)
                .filter(
                    (func.lower(Employee.emp_code) == clean_str)
                    | (func.lower(Employee.email) == clean_str)
                    | (func.lower(Employee.name) == clean_str)
                )
                .first()
            )

        if not emp:
            raise ValueError(f"User not found for identifier: {identifier}")

        subordinates = []
        if emp.role in ("manager", "hr_admin"):
            subordinates = get_all_subordinate_ids(session, emp.id)

        caller_ctx = CallerContext(
            employee_id=emp.id,
            emp_code=emp.emp_code,
            name=emp.name,
            email=emp.email,
            department=emp.department,
            designation=emp.designation,
            role=emp.role,
            status=emp.status,
            join_date=emp.join_date.isoformat() if emp.join_date else None,
            subordinate_ids=subordinates,
        )

        # Cache with 60-second TTL
        _set_in_cache(cache_key, caller_ctx)
        _set_in_cache(emp.emp_code.lower(), caller_ctx)
        _set_in_cache(str(emp.id), caller_ctx)
        _set_in_cache(emp.name.lower(), caller_ctx)

        return caller_ctx
    finally:
        session.close()


def get_jwt_secret() -> str:
    """Returns the JWT signing secret from settings, with DEMO_MODE fallback."""
    settings = get_settings()
    if settings.jwt_secret and settings.jwt_secret.strip():
        return settings.jwt_secret.strip()
    if settings.demo_mode:
        return "demo-default-jwt-secret-do-not-use-in-production"
    raise RuntimeError("JWT_SECRET is not configured in .env outside of DEMO_MODE.")


def _sync_get_current_caller(request: Request) -> CallerContext:
    """Synchronous implementation reading the cookie and loading caller from DB."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in.",
        )

    secret = get_jwt_secret()
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        employee_id = payload.get("employee_id")
        if not employee_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing employee_id.")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or corrupt authentication token.")

    now = time.time()
    emp_id_int = int(employee_id)
    token_v_claim = payload.get("token_version", 1)

    cached_entry = _AUTH_VERIFY_CACHE.get(emp_id_int)
    if cached_entry:
        cached_ctx, cached_tv, cached_mcp, cached_ts = cached_entry
        if (now - cached_ts < AUTH_CACHE_TTL) and (cached_tv == token_v_claim):
            if cached_mcp:
                req_path = request.url.path.rstrip("/")
                exempt_paths = {"/auth/me", "/auth/change-password", "/auth/logout"}
                if req_path not in exempt_paths and not any(req_path.endswith(p) for p in exempt_paths):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "PASSWORD_CHANGE_REQUIRED",
                            "message": "Password change required before accessing this resource.",
                        },
                    )
            return cached_ctx
        elif now - cached_ts >= AUTH_CACHE_TTL:
            _AUTH_VERIFY_CACHE.pop(emp_id_int, None)

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == emp_id_int).first()
        if not emp:
            raise HTTPException(status_code=401, detail="User not found.")

        # Rejects if status != 'active' or 'onboarding'
        if emp.status not in ("active", "onboarding"):
            raise HTTPException(
                status_code=401,
                detail=f"Account access denied: status is '{emp.status}'.",
            )

        must_change = False
        token_v = 1
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        if creds:
            token_v = creds.token_version
            token_claim = payload.get("token_version")
            if token_claim is None:
                if creds.token_version != 1:
                    raise HTTPException(status_code=401, detail="Token has been invalidated. Please log in again.")
            elif token_claim != creds.token_version:
                raise HTTPException(status_code=401, detail="Token has been invalidated. Please log in again.")

            must_change = bool(creds.must_change_password)
            if creds.must_change_password:
                req_path = request.url.path.rstrip("/")
                exempt_paths = {"/auth/me", "/auth/change-password", "/auth/logout"}
                if req_path not in exempt_paths and not any(req_path.endswith(p) for p in exempt_paths):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "PASSWORD_CHANGE_REQUIRED",
                            "message": "Password change required before accessing this resource.",
                        },
                    )

        subordinates = []
        if emp.role in ("manager", "hr_admin"):
            subordinates = get_all_subordinate_ids(session, emp.id)

        caller_ctx = CallerContext(
            employee_id=emp.id,
            emp_code=emp.emp_code,
            name=emp.name,
            email=emp.email,
            department=emp.department,
            designation=emp.designation,
            role=emp.role,
            status=emp.status,
            join_date=emp.join_date.isoformat() if emp.join_date else None,
            subordinate_ids=subordinates,
        )
        _AUTH_VERIFY_CACHE[emp.id] = (caller_ctx, token_v, must_change, now)
        return caller_ctx
    finally:
        session.close()


async def get_current_caller(request: Request) -> CallerContext:
    """Non-blocking FastAPI dependency that offloads DB and crypto operations to a thread."""
    return await asyncio.to_thread(_sync_get_current_caller, request)


def resolve_employee_target(
    session: Session,
    query_str: Optional[str],
    caller: CallerContext,
) -> Dict[str, Any]:
    """Resolves an employee query string (name, emp_code, or None) to an Employee object or candidates.

    Bug 3 & 4 fixes:
    - Runs the scope check BEFORE returning candidates.
    - Employees get only candidates within their scope (themselves) or a generic
      "multiple matches, use the employee code" message without names.
    - For non-HR callers, out-of-scope employees are indistinguishable from non-existent employees.

    Returns:
    - {"employee": Employee}: if uniquely resolved
    - {"candidates": [...]}: if ambiguous (scoped)
    - {"error": ...}: if not found or out of scope
    """
    if not query_str or not query_str.strip():
        # Defaults to caller
        emp = session.query(Employee).filter(Employee.id == caller.employee_id).first()
        return {"employee": emp}

    q = query_str.strip()

    # Try exact match by emp_code or id
    matched_emp = None
    if q.isdigit():
        matched_emp = session.query(Employee).filter(Employee.id == int(q)).first()
    elif q.upper().startswith("ACM-"):
        matched_emp = session.query(Employee).filter(Employee.emp_code.ilike(q)).first()

    if matched_emp:
        # Check scope if caller is not hr_admin
        if caller.role != "hr_admin" and not caller.can_access_employee(matched_emp.id):
            return {"error": f"Employee '{query_str}' not found in Acme Technologies directory."}
        return {"employee": matched_emp}

    # Match by full name or partial name
    matches = session.query(Employee).filter(Employee.name.ilike(f"%{q}%")).all()

    # Check if any exact name match first
    exact = [m for m in matches if m.name.lower() == q.lower()]
    if len(exact) == 1:
        target = exact[0]
        if caller.role != "hr_admin" and not caller.can_access_employee(target.id):
            return {"error": f"Employee '{query_str}' not found in Acme Technologies directory."}
        return {"employee": target}

    # Filter candidates by caller scope BEFORE returning
    if caller.role != "hr_admin":
        scoped_matches = [m for m in matches if caller.can_access_employee(m.id)]
    else:
        scoped_matches = matches

    if len(scoped_matches) == 1:
        return {"employee": scoped_matches[0]}
    elif len(scoped_matches) > 1:
        if caller.role == "employee":
            # Generic message without exposing names
            return {
                "ambiguous": True,
                "candidates": [],
                "message": f"Multiple employees matched '{query_str}'. Please use the employee code.",
            }
        return {
            "ambiguous": True,
            "candidates": [
                {"id": m.id, "emp_code": m.emp_code, "name": m.name, "department": m.department}
                for m in scoped_matches
            ],
            "message": f"Multiple employees matched '{query_str}'. Please specify emp_code or full name.",
        }

    # If matches existed in DB but were all out of scope, or no matches at all:
    # Do not reveal whether out-of-scope employees exist.
    return {"error": f"Employee '{query_str}' not found in Acme Technologies directory."}
