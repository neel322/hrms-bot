import bcrypt
from datetime import datetime, timedelta
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response
import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import func

from app.auth import (
    CallerContext,
    clear_caller_cache,
    get_current_caller,
    get_jwt_secret,
    hash_password,
    invalidate_auth_cache,
    verify_password,
)
from app.config import get_settings
from app.db import AuthCredentials, Employee, Invite, get_session, log_admin_audit
import hashlib
from app.rate_limiter import (
    activate_ip_limiter,
    demo_login_ip_limiter,
    get_client_ip,
    login_ip_limiter,
)

logger = logging.getLogger("hrms-bot.auth_routes")

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])

# Dummy bcrypt hash for constant-time email enumeration resistance
DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy_timing_salt", bcrypt.gensalt()).decode("utf-8")
GENERIC_LOGIN_ERROR = "Invalid email or password"


class LoginRequest(BaseModel):
    email: str
    password: str


class DemoLoginRequest(BaseModel):
    emp_code: str


class ActivateRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def set_auth_cookie(
    response: Response,
    request: Request,
    employee_id: int,
    token_version: Optional[int] = None,
) -> str:
    """Signs an 8-hour JWT carrying employee_id and token_version, and sets an HttpOnly cookie."""
    settings = get_settings()
    secret = get_jwt_secret()

    if token_version is None:
        session = get_session()
        try:
            creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == employee_id).first()
            token_version = creds.token_version if creds else 1
        finally:
            session.close()

    exp = datetime.utcnow() + timedelta(hours=8)
    token = jwt.encode(
        {"employee_id": employee_id, "token_version": token_version, "exp": exp},
        secret,
        algorithm="HS256",
    )

    host = request.url.hostname or ""
    is_localhost = host in ("localhost", "127.0.0.1", "testserver") or host.endswith(".local")
    if settings.cookie_secure is not None:
        secure_cookie = settings.cookie_secure
    else:
        secure_cookie = not is_localhost

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
        max_age=8 * 3600,
        path="/",
    )
    return token


@auth_router.get("/config")
async def get_auth_config():
    """Returns frontend authentication configuration (e.g. demo mode state)."""
    settings = get_settings()
    return {"demo_mode": settings.demo_mode}


def validate_password_rules(password: str, emp: Employee) -> None:
    """Enforces password lifecycle rules:
    - Minimum 10 characters
    - Maximum 72 bytes (bcrypt restriction)
    - Rejects full name (not 3-letter fragments)
    - Rejects full email address or full email username
    """
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password cannot exceed 72 bytes.")
    if len(password) < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters long.")

    pwd_lower = password.lower()
    full_name = emp.name.strip().lower() if emp.name else ""
    if full_name and (pwd_lower == full_name or full_name in pwd_lower):
        raise HTTPException(status_code=400, detail="Password cannot contain your full name.")

    if emp.email:
        email_clean = emp.email.strip().lower()
        if pwd_lower == email_clean:
            raise HTTPException(status_code=400, detail="Password cannot be your email address.")
        email_username = email_clean.split("@")[0]
        if email_username and (pwd_lower == email_username or email_username in pwd_lower):
            raise HTTPException(status_code=400, detail="Password cannot contain your email username.")


@auth_router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """Authenticates employee by email + password and sets HttpOnly JWT cookie.

    - Uses exact case-insensitive match (func.lower ==).
    - Locks account for 15 minutes after 5 failed attempts.
    - Constant-time bcrypt check against dummy hash if user doesn't exist.
    - Returns identical generic 401 error for unknown email, wrong password, inactive, and locked.
    - Resets failed_attempts when lock expires.
    - Applies per-IP rate limiting.
    """
    client_ip = get_client_ip(request)
    login_ip_limiter.enforce(client_ip, "Too many login attempts from this IP. Please try again later.")

    clean_email = req.email.strip().lower()
    session = get_session()
    try:
        emp = session.query(Employee).filter(func.lower(Employee.email) == clean_email).first()
        if not emp:
            verify_password(req.password, DUMMY_BCRYPT_HASH)
            logger.warning(f"Login failed: email '{clean_email}' not found.")
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        if not creds:
            verify_password(req.password, DUMMY_BCRYPT_HASH)
            logger.warning(f"Login failed: no credentials for '{clean_email}'.")
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        # Check account status
        if emp.status not in ("active", "onboarding"):
            verify_password(req.password, creds.password_hash)
            logger.warning(f"Login failed: account '{clean_email}' is {emp.status}.")
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        # Check if currently locked
        now = datetime.utcnow()
        if creds.locked_until:
            if creds.locked_until <= now:
                # Lock has expired: reset counters
                creds.failed_attempts = 0
                creds.locked_until = None
                session.commit()
            else:
                verify_password(req.password, creds.password_hash)
                logger.warning(f"Login failed: account '{clean_email}' is locked until {creds.locked_until}.")
                raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        # Verify password
        if not verify_password(req.password, creds.password_hash):
            creds.failed_attempts += 1
            if creds.failed_attempts >= 5:
                creds.locked_until = now + timedelta(minutes=15)
                logger.warning(f"Account for {emp.email} locked until {creds.locked_until} (5+ failed attempts).")
            session.commit()
            logger.warning(f"Login failed: incorrect password for '{clean_email}'. Failed attempts: {creds.failed_attempts}.")
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        # Login succeeded: reset counters
        creds.failed_attempts = 0
        creds.locked_until = None
        creds.last_login_at = now
        session.commit()

        set_auth_cookie(response, request, emp.id, token_version=creds.token_version)

        return {
            "message": "Login successful",
            "employee": {
                "id": emp.id,
                "emp_code": emp.emp_code,
                "name": emp.name,
                "email": emp.email,
                "department": emp.department,
                "designation": emp.designation,
                "role": emp.role,
                "must_change_password": creds.must_change_password,
            },
        }
    finally:
        session.close()


@auth_router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    response: Response,
    caller: CallerContext = Depends(get_current_caller),
):
    """Changes password for authenticated user, clears must_change_password, and invalidates old tokens."""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == caller.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found.")

        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        if not creds:
            raise HTTPException(status_code=400, detail="No credentials found for employee.")

        if not verify_password(req.current_password, creds.password_hash):
            raise HTTPException(status_code=400, detail="Incorrect current password.")

        validate_password_rules(req.new_password, emp)

        creds.password_hash = hash_password(req.new_password)
        creds.must_change_password = False
        creds.token_version += 1
        creds.failed_attempts = 0
        creds.locked_until = None
        session.commit()
        clear_caller_cache()
        invalidate_auth_cache(emp.id)
        set_auth_cookie(response, request, emp.id, token_version=creds.token_version)

        log_admin_audit(
            actor_emp_code=caller.emp_code,
            action="password_reset",
            target_emp_code=emp.emp_code,
            details={"type": "change_password"},
        )

        return {"message": "Password changed successfully."}
    finally:
        session.close()


@auth_router.post("/logout")
async def logout(request: Request, response: Response):
    """Logs out by incrementing token_version to invalidate tokens and clearing the cookie."""
    token = request.cookies.get("access_token")
    if token:
        try:
            secret = get_jwt_secret()
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            emp_id = payload.get("employee_id")
            if emp_id:
                session = get_session()
                try:
                    creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == int(emp_id)).first()
                    if creds:
                        creds.token_version += 1
                        session.commit()
                        clear_caller_cache()
                        invalidate_auth_cache(int(emp_id))
                finally:
                    session.close()
        except Exception:
            pass
    response.delete_cookie(key="access_token", path="/", samesite="lax")
    return {"message": "Logged out successfully"}


@auth_router.get("/me")
async def get_me(caller: CallerContext = Depends(get_current_caller)):
    """Returns caller profile and role."""
    session = get_session()
    must_change = False
    try:
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == caller.employee_id).first()
        if creds:
            must_change = creds.must_change_password
    finally:
        session.close()

    return {
        "id": caller.employee_id,
        "employee_id": caller.employee_id,
        "emp_code": caller.emp_code,
        "name": caller.name,
        "email": caller.email,
        "department": caller.department,
        "designation": caller.designation,
        "role": caller.role,
        "status": caller.status,
        "join_date": caller.join_date,
        "subordinate_ids": caller.subordinate_ids,
        "must_change_password": must_change,
    }


@auth_router.get("/session")
async def get_session_info(request: Request):
    """Safe session check returning {authenticated: bool} without raising HTTP 401 when no session exists."""
    token = request.cookies.get("access_token")
    if not token:
        return {"authenticated": False}
    try:
        caller = await get_current_caller(request)
        return {
            "authenticated": True,
            "caller": {
                "id": caller.employee_id,
                "employee_id": caller.employee_id,
                "emp_code": caller.emp_code,
                "name": caller.name,
                "email": caller.email,
                "department": caller.department,
                "designation": caller.designation,
                "role": caller.role,
                "status": caller.status,
                "join_date": caller.join_date,
                "subordinate_ids": caller.subordinate_ids,
            },
        }
    except Exception:
        return {"authenticated": False}


@auth_router.get("/demo-personas")
async def get_demo_personas():
    """Returns all active employees for persona switching. Exists ONLY when DEMO_MODE=true."""
    settings = get_settings()
    if not settings.demo_mode:
        raise HTTPException(
            status_code=404,
            detail="Demo personas endpoint is disabled in production.",
        )
    session = get_session()
    try:
        employees = (
            session.query(Employee)
            .filter(Employee.status.in_(["active", "onboarding"]))
            .order_by(Employee.id.asc())
            .all()
        )
        return [
            {
                "id": emp.id,
                "emp_code": emp.emp_code,
                "name": emp.name,
                "department": emp.department,
                "designation": emp.designation,
                "role": emp.role,
                "status": emp.status,
            }
            for emp in employees
        ]
    finally:
        session.close()


@auth_router.post("/demo-login")
async def demo_login(req: DemoLoginRequest, request: Request, response: Response):
    """Fast switch persona for presentation demos. Exists ONLY when DEMO_MODE=true."""
    settings = get_settings()
    if not settings.demo_mode:
        raise HTTPException(
            status_code=404,
            detail="Demo login is disabled in production.",
        )

    client_ip = get_client_ip(request)
    demo_login_ip_limiter.enforce(client_ip, "Too many demo login requests from this IP. Please try again later.")

    clean_code = req.emp_code.strip().lower()
    session = get_session()
    try:
        emp = session.query(Employee).filter(func.lower(Employee.emp_code) == clean_code).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Demo persona not found")

        if emp.status not in ("active", "onboarding"):
            raise HTTPException(
                status_code=403,
                detail=f"Cannot demo-login as employee with status '{emp.status}'.",
            )

        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        token_version = creds.token_version if creds else 1

        set_auth_cookie(response, request, emp.id, token_version=token_version)

        return {
            "message": f"Demo login as {emp.name} successful",
            "employee": {
                "id": emp.id,
                "emp_code": emp.emp_code,
                "name": emp.name,
                "email": emp.email,
                "department": emp.department,
                "designation": emp.designation,
                "role": emp.role,
            },
        }
    finally:
        session.close()


@auth_router.post("/activate")
async def activate_account(req: ActivateRequest, request: Request, response: Response):
    """Activates a new employee's account using an invite token and sets their password.

    - Validates token (hash match, not expired, not used).
    - Enforces password rules: >= 10 chars, <= 72 bytes, not equal to email or full name.
    - Stores bcrypt hash in auth_credentials.
    - Marks invite as used (used_at = now).
    - Sets must_change_password = False.
    - Logs user in (sets JWT cookie).
    - Applies per-IP rate limiting.
    """
    client_ip = get_client_ip(request)
    activate_ip_limiter.enforce(client_ip, "Too many activation attempts from this IP. Please try again later.")

    token_str = req.token.strip()
    if not token_str:
        raise HTTPException(status_code=400, detail="Invite token is required.")

    token_hash = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
    session = get_session()
    try:
        invite = session.query(Invite).filter(Invite.token_hash == token_hash).first()
        if not invite:
            raise HTTPException(status_code=400, detail="Invalid activation token.")

        if invite.used_at is not None:
            raise HTTPException(status_code=400, detail="This activation token has already been used.")

        now = datetime.utcnow()
        if invite.expires_at < now:
            raise HTTPException(status_code=400, detail="Activation token has expired. Please request a new invite.")

        emp = session.query(Employee).filter(Employee.id == invite.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found.")

        # Password validation rules
        pwd = req.new_password
        validate_password_rules(pwd, emp)

        # Update or create auth_credentials
        creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
        if not creds:
            creds = AuthCredentials(
                employee_id=emp.id,
                password_hash=hash_password(pwd),
                must_change_password=False,
                token_version=1,
                failed_attempts=0,
                last_login_at=now,
            )
            session.add(creds)
        else:
            creds.password_hash = hash_password(pwd)
            creds.must_change_password = False
            creds.failed_attempts = 0
            creds.locked_until = None
            creds.last_login_at = now

        # Mark invite used
        invite.used_at = now
        session.commit()
        clear_caller_cache()
        invalidate_auth_cache(emp.id)

        # Log in the user by setting auth cookie
        set_auth_cookie(response, request, emp.id, token_version=creds.token_version)

        log_admin_audit(
            actor_emp_code=emp.emp_code,
            action="password_reset",
            target_emp_code=emp.emp_code,
            details={"type": "account_activation"},
        )

        logger.info(f"Employee {emp.emp_code} ({emp.name}) activated their account successfully.")

        return {
            "message": "Account activated successfully. Welcome to Acme Technologies!",
            "employee": {
                "id": emp.id,
                "emp_code": emp.emp_code,
                "name": emp.name,
                "email": emp.email,
                "department": emp.department,
                "designation": emp.designation,
                "role": emp.role,
                "status": emp.status,
            },
        }
    finally:
        session.close()
