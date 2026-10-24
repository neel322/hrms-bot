import calendar
from datetime import date, datetime, timedelta
import re
from typing import Optional, Tuple
import zoneinfo

from app.config import get_settings


def get_current_date() -> date:
    """Returns 'today' in the configured timezone (TZ_NAME, default Asia/Kolkata)."""
    settings = get_settings()
    try:
        tz = zoneinfo.ZoneInfo(settings.tz_name)
        return datetime.now(tz).date()
    except Exception:
        pass
    try:
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        return datetime.now(tz).date()
    except Exception:
        # Fallback to UTC+5:30 (IST)
        from datetime import timezone
        tz = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(tz).date()


def get_current_datetime() -> datetime:
    """Returns current datetime in the configured timezone."""
    settings = get_settings()
    try:
        tz = zoneinfo.ZoneInfo(settings.tz_name)
        return datetime.now(tz)
    except Exception:
        pass
    try:
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        return datetime.now(tz)
    except Exception:
        from datetime import timezone
        tz = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(tz)


def add_months(d: date, n: int) -> date:
    """Adds (or subtracts) n months to date d, clamping the day to the last day of the target month.

    Examples:
    - Aug 31 + 6 months -> Feb 28 (or Feb 29 in leap year)
    - Jan 31 + 1 month -> Feb 28 (or Feb 29)
    - Mar 31 + 1 month -> Apr 30
    - Feb 29, 2024 + 12 months -> Feb 28, 2025
    """
    total_months = d.year * 12 + (d.month - 1) + n
    target_year = total_months // 12
    target_month = (total_months % 12) + 1
    max_days = calendar.monthrange(target_year, target_month)[1]
    target_day = min(d.day, max_days)
    return date(target_year, target_month, target_day)


def parse_date_range(range_str: Optional[str], ref_date: Optional[date] = None) -> Tuple[date, date]:
    """Parses natural language or explicit date range strings into (start_date, end_date).

    Supports:
    - None / "" / "today" -> (today, today)
    - "yesterday" -> (yesterday, yesterday)
    - "tomorrow" -> (tomorrow, tomorrow)
    - "this week" -> (Monday of this week, Sunday of this week)
    - "next week" -> (Monday of next week, Sunday of next week)
    - "last week" -> (Monday of last week, Sunday of last week)
    - "this month" -> (first day of this month, last day of this month)
    - "last month" -> (first day of last month, last day of last month)
    - "next month" -> (first day of next month, last day of next month)
    - "this year" -> (first day of this year, last day of this year)
    - Month names e.g. "September", "sep 2026", "Oct 2025" -> (first day of month, last day of month)
    - "last N days" e.g. "last 7 days", "last 30 days" -> (today - N days, today)
    - Single date "YYYY-MM-DD" -> (date, date)
    - Range "YYYY-MM-DD to YYYY-MM-DD" or "YYYY-MM-DD - YYYY-MM-DD" -> (start, end)

    Raises ValueError with descriptive message for invalid or unparseable inputs.
    """
    today = ref_date or get_current_date()

    if not range_str or not str(range_str).strip():
        return today, today

    s = str(range_str).strip().lower()

    if s == "today":
        return today, today

    if s == "yesterday":
        yest = today - timedelta(days=1)
        return yest, yest

    if s == "tomorrow":
        tmr = today + timedelta(days=1)
        return tmr, tmr

    if s == "this week":
        start = today - timedelta(days=today.weekday())  # Monday
        end = start + timedelta(days=6)  # Sunday
        return start, end

    if s == "next week":
        start = today + timedelta(days=(7 - today.weekday()))  # Next Monday
        end = start + timedelta(days=6)  # Next Sunday
        return start, end

    if s == "last week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday

    if s == "this month":
        start = date(today.year, today.month, 1)
        _, last_day = calendar.monthrange(today.year, today.month)
        end = date(today.year, today.month, last_day)
        return start, end

    if s == "last month":
        if today.month == 1:
            prev_y, prev_m = today.year - 1, 12
        else:
            prev_y, prev_m = today.year, today.month - 1
        _, last_d = calendar.monthrange(prev_y, prev_m)
        return date(prev_y, prev_m, 1), date(prev_y, prev_m, last_d)

    if s == "next month":
        if today.month == 12:
            next_y, next_m = today.year + 1, 1
        else:
            next_y, next_m = today.year, today.month + 1
        _, last_d = calendar.monthrange(next_y, next_m)
        return date(next_y, next_m, 1), date(next_y, next_m, last_d)

    if s == "this year":
        return date(today.year, 1, 1), date(today.year, 12, 31)

    # Last N days (e.g. "last 7 days", "last 30 days")
    last_n_match = re.match(r"^last\s+(\d+)\s+days?$", s)
    if last_n_match:
        n = int(last_n_match.group(1))
        if n < 0:
            raise ValueError(f"Days count cannot be negative in '{range_str}'.")
        start = today - timedelta(days=n)
        return start, today

    # Month names (e.g. "September", "sep 2026", "October 2026")
    month_match = re.match(
        r"^(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(\d{4}))?$",
        s,
    )
    if month_match:
        m_name = month_match.group(1).lower()
        month_map = {
            "jan": 1, "january": 1,
            "feb": 2, "february": 2,
            "mar": 3, "march": 3,
            "apr": 4, "april": 4,
            "may": 5,
            "jun": 6, "june": 6,
            "jul": 7, "july": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10,
            "nov": 11, "november": 11,
            "dec": 12, "december": 12,
        }
        month_num = month_map.get(m_name)
        if month_num:
            year_val = int(month_match.group(2)) if month_match.group(2) else today.year
            _, last_d = calendar.monthrange(year_val, month_num)
            return date(year_val, month_num, 1), date(year_val, month_num, last_d)

    # Range format: YYYY-MM-DD to YYYY-MM-DD or YYYY-MM-DD - YYYY-MM-DD
    range_match = re.match(
        r"^(\d{4}-\d{2}-\d{2})\s*(?:to|--|-)\s*(\d{4}-\d{2}-\d{2})$",
        s,
    )
    if range_match:
        try:
            start = datetime.strptime(range_match.group(1), "%Y-%m-%d").date()
            end = datetime.strptime(range_match.group(2), "%Y-%m-%d").date()
            if start > end:
                raise ValueError(f"Start date ({start}) cannot be after end date ({end}) in date range.")
            return start, end
        except ValueError as e:
            if "Start date" in str(e):
                raise
            raise ValueError(f"Invalid date format in range '{range_str}': {e}")

    # Single date: YYYY-MM-DD
    single_match = re.match(r"^(\d{4}-\d{2}-\d{2})$", s)
    if single_match:
        try:
            d = datetime.strptime(single_match.group(1), "%Y-%m-%d").date()
            return d, d
        except ValueError as e:
            raise ValueError(f"Invalid date format '{range_str}': {e}")

    raise ValueError(
        f"Unrecognized date range format: '{range_str}'. "
        f"Expected 'today', 'yesterday', 'tomorrow', 'this week', 'last week', 'next week', "
        f"'this month', 'last month', 'next month', 'this year', month name (e.g. 'September', 'sep 2026'), "
        f"'last N days', 'YYYY-MM-DD', or 'YYYY-MM-DD to YYYY-MM-DD'."
    )


# Sensitive memory keywords with word boundaries
SENSITIVE_PATTERNS = [
    r"\bsalary\b",
    r"\bpayroll\b",
    r"\bnet\s*pay\b",
    r"\binr\b",
    r"\bctc\b",
    r"\bbank\b",
    r"\baccount\s*number\b",
    r"\bpan\b",
    r"\baadhaar\b",
    r"\bmedical\b",
    r"\bdiagnosis\b",
    r"\bgrievance\b",
    r"\bposh\b",
    r"\bdisciplinary\b",
    r"\btermination\b",
    r"\bhealth\b",
    r"\billness\b",
    r"\bpregnancy\b",
    r"\bpregnant\b",
    r"\bvisa\b",
    r"\breligion\b",
    r"\breligious\b",
    r"\bcaste\b",
    r"\bsexual\s*orientation\b",
    r"\blawsuit\b",
    r"\blitigation\b",
    r"\bdisability\b",
    r"\bdisabled\b",
    r"\bmental\b",
]

_COMPILED_SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS), re.IGNORECASE)


def is_sensitive_memory(text: str) -> bool:
    """Checks if the text contains sensitive financial, health, identity, or disciplinary content.

    Uses word-boundary regex patterns to avoid false substring matches.
    """
    if not text:
        return False
    return bool(_COMPILED_SENSITIVE_RE.search(text))
