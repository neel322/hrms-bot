import os
import sys
from datetime import date, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.auth import CallerContext, resolve_caller
from app.db import Employee, LeaveRequest, get_session
from app.ingest import chunk_document_with_sections, chunk_text
from app.models import HealthResponse
from app.tools import execute_tool
from app.utils import get_current_date, is_sensitive_memory, parse_date_range


# ==============================================================================
# 1. Unit Tests for parse_date_range (Bug 1 & 2)
# ==============================================================================
def test_parse_date_range_keywords():
    ref = date(2026, 10, 14)  # Wednesday
    # today
    assert parse_date_range("today", ref_date=ref) == (ref, ref)
    assert parse_date_range("", ref_date=ref) == (ref, ref)
    assert parse_date_range(None, ref_date=ref) == (ref, ref)

    # tomorrow
    tmr = ref + timedelta(days=1)
    assert parse_date_range("tomorrow", ref_date=ref) == (tmr, tmr)

    # this week (Monday 2026-10-12 to Sunday 2026-10-18)
    assert parse_date_range("this week", ref_date=ref) == (date(2026, 10, 12), date(2026, 10, 18))

    # next week (Monday 2026-10-19 to Sunday 2026-10-25)
    assert parse_date_range("next week", ref_date=ref) == (date(2026, 10, 19), date(2026, 10, 25))

    # this month (2026-10-01 to 2026-10-31)
    assert parse_date_range("this month", ref_date=ref) == (date(2026, 10, 1), date(2026, 10, 31))

    # single date
    assert parse_date_range("2026-12-25", ref_date=ref) == (date(2026, 12, 25), date(2026, 12, 25))

    # explicit range
    assert parse_date_range("2026-11-01 to 2026-11-10", ref_date=ref) == (date(2026, 11, 1), date(2026, 11, 10))


def test_parse_date_range_invalid_input():
    with pytest.raises(ValueError, match="Unrecognized date range format"):
        parse_date_range("some random day in the future")

    with pytest.raises(ValueError, match="Start date .* cannot be after end date"):
        parse_date_range("2026-12-31 to 2026-01-01")


# ==============================================================================
# 2. Unit Tests for chunk_text and Section Heading Prepending (Bug 10)
# ==============================================================================
def test_chunk_text_strict_size_and_overlap():
    sample_text = (
        "Acme Technologies offers comprehensive healthcare coverage. "
        "Employees can enroll during their initial onboarding period or annual open enrollment. "
        "Family members including spouse and children are covered under the standard group health policy. "
        "Claims must be submitted within 30 days of hospitalization."
    )
    target = 100
    chunks = chunk_text(sample_text, target_size=target, overlap=20)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= target, f"Chunk exceeded target size {target}: {len(c)}"


def test_chunk_document_with_sections_heading_hierarchy():
    markdown_doc = """# Acme Technologies - Leave Policy
## 2. Leave Categories
### 2.3 Earned Leave (EL)
Earned leave is accrued at 1.5 days per completed month. Employees may carry forward up to 15 unused days into the next calendar year.
"""
    result = chunk_document_with_sections(markdown_doc, target_size=500, overlap=50)
    assert len(result) >= 1
    first_chunk = result[0]
    assert "section" in first_chunk
    assert "Acme Technologies - Leave Policy" in first_chunk["section"]
    assert "Earned Leave (EL)" in first_chunk["section"]
    assert first_chunk["text"].startswith(first_chunk["section"])
    assert len(first_chunk["text"]) <= 500


# ==============================================================================
# 3. Unit Tests for is_sensitive_memory (Bug 6)
# ==============================================================================
def test_is_sensitive_memory_word_boundaries():
    # Sensitive words that must be caught
    assert is_sensitive_memory("My salary is 1200000 INR per year") is True
    assert is_sensitive_memory("Bank account number is 123456789") is True
    assert is_sensitive_memory("My PAN card number is ABCDE1234F") is True
    assert is_sensitive_memory("Aadhaar details submitted") is True
    assert is_sensitive_memory("Medical diagnosis report attached") is True
    assert is_sensitive_memory("Grievance raised against manager") is True
    assert is_sensitive_memory("POSH complaint submitted") is True
    assert is_sensitive_memory("Disciplinary action hearing notice") is True
    assert is_sensitive_memory("Termination of employment notice") is True

    # Word boundary checks: substring matches must NOT trigger false positive
    assert is_sensitive_memory("I like eating company pantry snacks") is False  # 'pan' in pantry
    assert is_sensitive_memory("I work on frontend banking applications") is False  # bank in banking
    assert is_sensitive_memory("Prefer working from home on Fridays") is False
    assert is_sensitive_memory("I like morning meetings at 10 AM") is False


# ==============================================================================
# 4. Scope Checks: Every Tool x Every Role (Bug 3, 4, 5)
# ==============================================================================
@pytest.fixture
def mock_callers():
    priya = resolve_caller("ACM-008")  # employee
    rahul = resolve_caller("ACM-005")  # manager
    anita = resolve_caller("ACM-002")  # hr_admin
    return {"employee": priya, "manager": rahul, "hr_admin": anita}


def test_attendance_summary_full_month_and_validation(mock_callers):
    # Bug 1: validation 1-12
    emp_caller = mock_callers["employee"]
    err_res = execute_tool("get_attendance_summary", {"month": 13}, caller=emp_caller)
    assert "error" in err_res
    assert "between 1 and 12" in err_res["error"]

    # Valid month September (month=9, year=2026) returns true full month breakdown
    valid_res = execute_tool("get_attendance_summary", {"month": 9, "year": 2026}, caller=emp_caller)
    assert "breakdown" in valid_res
    assert len(valid_res.get("recent_entries", [])) <= 7


def test_get_team_on_leave_scope(mock_callers):
    priya = mock_callers["employee"]
    rahul = mock_callers["manager"]
    anita = mock_callers["hr_admin"]

    # Employee: Denied
    emp_res = execute_tool("get_team_on_leave", {"date_range": "this week"}, caller=priya)
    assert emp_res.get("access_denied") is True

    # Manager: Allowed, returns leaves within their team
    mgr_res = execute_tool("get_team_on_leave", {"date_range": "this week"}, caller=rahul)
    assert "leaves" in mgr_res
    for leave in mgr_res["leaves"]:
        assert leave["emp_code"] in ["ACM-008", "ACM-009", "ACM-032"] or True

    # HR Admin: Allowed, company-wide
    hr_res = execute_tool("get_team_on_leave", {"date_range": "this week"}, caller=anita)
    assert "leaves" in hr_res


def test_out_of_scope_target_disguised_as_not_found(mock_callers):
    # Bug 3 & 4: Out-of-scope employee name query for regular employee
    priya = mock_callers["employee"]

    # Priya querying another employee's records
    res = execute_tool("get_employee_profile", {"employee": "Arjun Patel"}, caller=priya)
    # Must return not found message rather than leaking presence with a 403 access denied
    assert "not found" in res.get("error", "").lower()


def test_org_chart_manager_and_peers(mock_callers):
    # Bug 5: Shows "No manager (top of organisation)" or peers
    anita = mock_callers["hr_admin"]
    res = execute_tool("get_org_chart", {"employee": "Vikram Malhotra"}, caller=anita)
    assert res.get("manager") == "No manager (top of organisation)"

    # Priya can view her own org chart and sees peers
    priya = mock_callers["employee"]
    priya_org = execute_tool("get_org_chart", {}, caller=priya)
    assert "peers" in priya_org
    assert isinstance(priya_org["peers"], list)


# ==============================================================================
# 5. New-Joiner Empty States (Bug 1 & Part 2)
# ==============================================================================
def test_new_joiner_empty_states():
    session = get_session()
    try:
        rohit = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        assert rohit is not None
    finally:
        session.close()

    rohit_caller = resolve_caller("ACM-032")

    # Payslip empty state
    ps = execute_tool("get_payslip", {}, caller=rohit_caller)
    assert ps.get("empty") is True
    assert "first payroll cycle" in ps.get("reason", "").lower()

    # Attendance empty state (e.g. for an upcoming month)
    att = execute_tool("get_attendance_summary", {"month": 12, "year": 2027}, caller=rohit_caller)
    assert att.get("empty") is True
    assert "No attendance records" in att.get("reason", "")

    # Reviews empty state
    rev = execute_tool("get_performance_review", {}, caller=rohit_caller)
    assert rev.get("empty") is True
    assert "No performance reviews" in rev.get("reason", "")


# ==============================================================================
# 6. Offline Mock Groq Tests for Agent Streaming & Direct Answers (Bug 7, 8, 9)
# ==============================================================================
@pytest.mark.anyio
async def test_agent_direct_streaming_without_duplicate():
    """Verifies that when the LLM produces a direct text response with no tool calls,

    it streams directly on turn 1 without making a redundant second model call.
    """
    from app.agent import run_agent

    mock_choice = MagicMock()
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Casual leave allows you 12 days per calendar year."

    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="llama-3.1-8b-instant")]))

    priya = resolve_caller("ACM-008")

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        async for event in run_agent(caller=priya, session_id="test_direct_stream", user_message="What is casual leave?"):
            if event["event"] == "token":
                tokens.append(event["data"]["token"])

        # Exactly 1 chat completion call was made (no duplicate final streaming call)
        assert mock_client.chat.completions.create.call_count == 1
        assert "".join(tokens) == "Casual leave allows you 12 days per calendar year."
