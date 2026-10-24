import re
from datetime import date
from typing import Any, Dict, Optional

from app.auth import CallerContext
from app.utils import get_current_date


# Patterns for deterministic routing
LEAVE_BALANCE_RE = re.compile(
    r"\b(?:check\s+my\s+leaves?|my\s+leaves?|leave\s+balance|remaining\s+leaves?|casual\s+leaves?|sick\s+leaves?|earned\s+leaves?|how\s+many\s+(?:casual\s+|sick\s+|earned\s+)?leaves?\s+(?:do\s+i\s+have|left))\b",
    re.IGNORECASE,
)

HOLIDAYS_RE = re.compile(
    r"\b(?:upcoming\s+holidays?|next\s+holiday|holidays?\s+this\s+year|holiday\s+calendar|holiday\s+list|company\s+holidays?|when\s+is\s+the\s+next\s+holiday)\b",
    re.IGNORECASE,
)

PAYSLIP_RE = re.compile(
    r"\b(?:(?:my\s+)?(?:last\s+|latest\s+)?(?:payslip|salary\s+slip)|salary\s+breakdown|show\s+my\s+payslip|download\s+payslip)\b",
    re.IGNORECASE,
)

ATTENDANCE_RE = re.compile(
    r"\b(?:(?:my\s+)?attendance(?:\s+summary|\s+status|\s+records?)?|check\s+my\s+attendance|attendance\s+this\s+month|attendance\s+last\s+month)\b",
    re.IGNORECASE,
)

PROFILE_MANAGER_RE = re.compile(
    r"\b(?:who\s+is\s+my\s+manager|who\s+do\s+i\s+report\s+to|my\s+manager|reporting\s+manager|my\s+profile|my\s+employee\s+profile|my\s+details|my\s+designation)\b",
    re.IGNORECASE,
)

TEAM_ON_LEAVE_RE = re.compile(
    r"\b(?:who(?:'s|\s+is)\s+on\s+leave|team\s+on\s+leave|anyone\s+on\s+leave|who\s+all\s+are\s+on\s+leave)\b",
    re.IGNORECASE,
)

HEADCOUNT_RE = re.compile(
    r"\b(?:(?:company\s+|total\s+|workforce\s+)?headcount(?:\s+stats|\s+numbers?)?|how\s+many\s+employees\s+(?:work\s+here|in\s+the\s+company))\b",
    re.IGNORECASE,
)

MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def route_intent(message: str, caller: CallerContext) -> Optional[Dict[str, Any]]:
    """Determines if a user message matches a deterministic fast path.

    Returns a dict with 'name' and 'arguments' for execute_tool, or None if unrecognized.
    Does NOT match queries about policies, rules, or procedures.
    """
    if not message or not message.strip():
        return None

    msg = message.strip()
    msg_lower = msg.lower()

    # Rule out explicit policy inquiries
    if re.search(r"\b(?:policy|policies|rules?|guidelines?|handbook|procedure|posh|wfh\s+policy)\b", msg_lower):
        return None

    # 1. Leave balance
    if LEAVE_BALANCE_RE.search(msg_lower):
        return {
            "name": "get_leave_balance",
            "arguments": {},
        }

    # 2. Upcoming holidays
    if HOLIDAYS_RE.search(msg_lower):
        return {
            "name": "get_upcoming_holidays",
            "arguments": {"n": 5},
        }

    # 3. Payslip
    if PAYSLIP_RE.search(msg_lower):
        today = get_current_date()
        args: Dict[str, Any] = {}
        # Check if a specific month was mentioned
        for m_name, m_num in MONTH_MAP.items():
            if re.search(rf"\b{m_name}\b", msg_lower):
                args["month"] = m_num
                break
        year_match = re.search(r"\b(202[0-9])\b", msg_lower)
        if year_match:
            args["year"] = int(year_match.group(1))
        elif "month" in args:
            args["year"] = today.year
        return {
            "name": "get_payslip",
            "arguments": args,
        }

    # 4. Attendance
    if ATTENDANCE_RE.search(msg_lower):
        today = get_current_date()
        args: Dict[str, Any] = {}
        if "last month" in msg_lower:
            target_month = today.month - 1 if today.month > 1 else 12
            target_year = today.year if today.month > 1 else today.year - 1
            args["month"] = target_month
            args["year"] = target_year
        elif "this month" in msg_lower:
            args["month"] = today.month
            args["year"] = today.year
        else:
            for m_name, m_num in MONTH_MAP.items():
                if re.search(rf"\b{m_name}\b", msg_lower):
                    args["month"] = m_num
                    args["year"] = today.year
                    break
        return {
            "name": "get_attendance_summary",
            "arguments": args,
        }

    # 5. Manager / Profile
    if PROFILE_MANAGER_RE.search(msg_lower):
        return {
            "name": "get_employee_profile",
            "arguments": {},
        }

    # 6. Who is on leave (Manager / HR Admin)
    if TEAM_ON_LEAVE_RE.search(msg_lower):
        if caller.role in ("manager", "hr_admin"):
            date_range = "today"
            if "next week" in msg_lower:
                date_range = "next week"
            elif "this week" in msg_lower or "week" in msg_lower:
                date_range = "this week"
            elif "tomorrow" in msg_lower:
                date_range = "tomorrow"
            return {
                "name": "get_team_on_leave",
                "arguments": {"date_range": date_range},
            }

    # 7. Headcount (HR Admin only)
    if HEADCOUNT_RE.search(msg_lower):
        if caller.role == "hr_admin":
            return {
                "name": "get_headcount_stats",
                "arguments": {},
            }

    return None
