import asyncio
import calendar
from datetime import date, datetime, timedelta
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from groq import GroqError
import jwt
from fastapi.testclient import TestClient

from app.agent import is_policy_question, reset_model_cache, run_agent, strip_think_tags
from app.auth import CallerContext, get_jwt_secret, resolve_caller
from app.config import get_settings
from app.db import (
    AuthCredentials,
    ChatHistory,
    Employee,
    LeaveRequest,
    add_chat_message,
    get_chat_history,
    get_session,
    init_db,
)
from app.main import app
from app.tools import execute_tool
from app.utils import get_current_date, get_current_datetime, is_sensitive_memory, parse_date_range


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()


@pytest.fixture(autouse=True)
def reset_agent_cache():
    reset_model_cache()
    yield
    reset_model_cache()


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
# 1. FAILED TURNS & RETRY
# ==============================================================================
@pytest.mark.anyio
async def test_failed_turn_stores_status_failed_and_empty_content():
    """On error, assistant message is stored with content='' and status='failed',

    never '[Error: ...] (failed)'. Excluded by get_chat_history and /history.
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_failed_turn_{int(time.time() * 1000)}"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=GroqError("Internal API rate limit"))
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        events = []
        async for event in run_agent(caller=priya, session_id=session_id, user_message="Check my sick leaves"):
            events.append(event)

        # Event stream reported error
        assert any(e["event"] == "error" for e in events)

        # Inspect DB rows directly
        db = get_session()
        try:
            records = db.query(ChatHistory).filter(ChatHistory.session_id == session_id).order_by(ChatHistory.id.asc()).all()
            assert len(records) == 2
            user_rec, asst_rec = records[0], records[1]

            assert user_rec.role == "user"
            assert user_rec.content == "Check my sick leaves"
            assert user_rec.status == "ok"

            assert asst_rec.role == "assistant"
            assert asst_rec.content == ""  # Never '[Error: ...] (failed)'
            assert asst_rec.status == "failed"
            assert asst_rec.error_code == "GroqError"
        finally:
            db.close()

        # get_chat_history with exclude_failed=True must exclude the failed assistant turn
        history = get_chat_history(session_id=session_id, exclude_failed=True)
        assert len(history) == 1
        assert history[0]["role"] == "user"


@pytest.mark.anyio
async def test_retry_deletes_failed_row_and_reuses_user_row():
    """Retrying with retry=true removes the failed assistant row and reuses the existing

    user row instead of inserting a duplicate user row.
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_retry_{int(time.time() * 1000)}"

    # 1. Seed user message and failed assistant turn
    add_chat_message(session_id, "user", "What is the reimbursement process?", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "", user_id=priya.emp_code, status="failed", error_code="GroqError")

    # 2. Mock a successful reply on retry
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Submit your bills via the Acme HRMS expense portal within 30 days."

    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        async for event in run_agent(
            caller=priya,
            session_id=session_id,
            user_message="What is the reimbursement process?",
            retry=True,
        ):
            if event["event"] == "token":
                tokens.append(event["data"]["token"])

        assert "".join(tokens) == "Submit your bills via the Acme HRMS expense portal within 30 days."

        # DB must have exactly 1 user row and 1 assistant row (no duplicate user row!)
        db = get_session()
        try:
            records = db.query(ChatHistory).filter(ChatHistory.session_id == session_id).order_by(ChatHistory.id.asc()).all()
            assert len(records) == 2
            assert records[0].role == "user"
            assert records[0].content == "What is the reimbursement process?"
            assert records[1].role == "assistant"
            assert records[1].status == "ok"
            assert records[1].content == "Submit your bills via the Acme HRMS expense portal within 30 days."
        finally:
            db.close()


# ==============================================================================
# 2. REASONING MODEL & DEFENSIVE THINK STRIPPING
# ==============================================================================
@pytest.mark.anyio
async def test_reasoning_parameters_and_think_tag_stripping():
    """Sets reasoning_format='hidden', lowest reasoning effort for tool routing,

    and defensively strips <think>...</think> tags from output and streaming.
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_think_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    # Model generates internal thinking enclosed in <think> tags
    mock_choice.message.content = "<think>\nThinking about employee leave entitlement...\n</think>You are entitled to 12 casual leaves."

    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        async for event in run_agent(caller=priya, session_id=session_id, user_message="Tell me about casual leave"):
            if event["event"] == "token":
                tokens.append(event["data"]["token"])

        full_output = "".join(tokens)
        assert "<think>" not in full_output
        assert "</think>" not in full_output
        assert "Thinking about employee leave" not in full_output
        assert "You are entitled to 12 casual leaves." in full_output

        # Verify call kwargs included reasoning parameters
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("reasoning_format") == "hidden"
        assert call_kwargs.get("reasoning_effort") == "none"
        assert call_kwargs.get("max_tokens") in (300, 600)


@pytest.mark.anyio
async def test_finish_reason_length_retries_once_with_larger_budget():
    """If a response ends with finish_reason == 'length', retries once with a larger budget."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_length_{int(time.time() * 1000)}"

    first_choice = MagicMock()
    first_choice.finish_reason = "length"
    first_choice.message.tool_calls = None
    first_choice.message.content = "Truncated text..."
    first_resp = MagicMock(choices=[first_choice])

    second_choice = MagicMock()
    second_choice.finish_reason = "stop"
    second_choice.message.tool_calls = None
    second_choice.message.content = "Full expanded answer after budget increase."
    second_resp = MagicMock(choices=[second_choice])

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[first_resp, second_resp])
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        async for event in run_agent(caller=priya, session_id=session_id, user_message="Tell me full details"):
            if event["event"] == "token":
                tokens.append(event["data"]["token"])

        assert "".join(tokens) == "Full expanded answer after budget increase."
        assert mock_client.chat.completions.create.call_count == 2
        # Second call had doubled max_tokens
        call_1_tokens = mock_client.chat.completions.create.call_args_list[0].kwargs["max_tokens"]
        call_2_tokens = mock_client.chat.completions.create.call_args_list[1].kwargs["max_tokens"]
        assert call_2_tokens == call_1_tokens * 2


@pytest.mark.anyio
async def test_fallback_model_used_when_primary_errors():
    """Uses GROQ_FALLBACK_MODEL automatically when primary model errors or is retired."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_fallback_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Answer from fallback model."
    fallback_resp = MagicMock(choices=[mock_choice])

    # First call with primary model fails with 429 / decommissioned
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[
        GroqError("Model qwen/qwen3.8-27b has been decommissioned"),
        fallback_resp,
    ])
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[
        MagicMock(id="qwen/qwen3.8-27b"),
        MagicMock(id=get_settings().groq_fallback_model),
    ]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        done_data = None
        async for event in run_agent(caller=priya, session_id=session_id, user_message="Hello"):
            if event["event"] == "token":
                tokens.append(event["data"]["token"])
            elif event["event"] == "done":
                done_data = event["data"]

        assert "".join(tokens) == "Answer from fallback model."
        assert done_data["model"] == get_settings().groq_fallback_model


# ==============================================================================
# 3. FORCE GROUNDING FOR POLICY ANSWERS
# ==============================================================================
@pytest.mark.anyio
async def test_policy_grounding_preemptively_searches_and_injects_evidence():
    """Detects policy questions, searches policies pre-emptively, emits tool_call/tool_result,

    and injects top 4 chunks into context.
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_grounding_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "According to leave_policy.md, casual leave is 12 days."
    mock_resp = MagicMock(choices=[mock_choice])

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    mock_policy_results = {
        "query": "What is the probation policy?",
        "results": [
            {"source": "onboarding_guide.md", "section": "Probation", "score": 0.88, "text": "Probation is 6 months."},
            {"source": "leave_policy.md", "section": "Probation Leave", "score": 0.75, "text": "Leaves during probation."},
        ]
    }

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.search_policies", return_value=mock_policy_results):
        events = []
        async for event in run_agent(caller=priya, session_id=session_id, user_message="What is the probation policy?"):
            events.append(event)

        # Asserts tool_call and tool_result events were emitted for search_policies
        tc = next((e for e in events if e["event"] == "tool_call" and e["data"]["name"] == "search_policies"), None)
        tr = next((e for e in events if e["event"] == "tool_result" and e["data"]["name"] == "search_policies"), None)
        assert tc is not None
        assert tr is not None

        # Verify messages passed to LLM had the injected policy chunks
        first_call_msgs = mock_client.chat.completions.create.call_args.kwargs["messages"]
        injected_tool_msg = next((m for m in first_call_msgs if m.get("role") == "tool" and m.get("name") == "search_policies"), None)
        assert injected_tool_msg is not None
        assert "Probation is 6 months." in injected_tool_msg["content"]


@pytest.mark.anyio
async def test_policy_grounding_low_score_instructs_contact_hr():
    """If no policy chunk scores >= POLICY_MIN_SCORE (0.45), instruct model to reply

    'I couldn't find that in the HR policies, please contact HR.'
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_low_score_{int(time.time() * 1000)}"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock()
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    # All chunks have score below 0.45 threshold
    mock_low_scores = {
        "query": "Can we bring pets to office?",
        "results": [
            {"source": "code_of_conduct.md", "section": "Pets", "score": 0.22, "text": "Unrelated text"},
        ]
    }

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.search_policies", return_value=mock_low_scores):
        async for _ in run_agent(caller=priya, session_id=session_id, user_message="Can I bring my pet to work under office rules?"):
            pass

        # Inspect injected tool message instruction
        first_call_msgs = mock_client.chat.completions.create.call_args.kwargs["messages"]
        injected_tool = next((m for m in first_call_msgs if m.get("role") == "tool" and m.get("name") == "search_policies"), None)
        assert injected_tool is not None
        content_obj = json.loads(injected_tool["content"])
        assert "I couldn't find that in the HR policies, please contact HR." in content_obj.get("instruction", "")


# ==============================================================================
# 4. MEMORY ACTUALLY USED & EXTRACTED IN BACKGROUND
# ==============================================================================
@pytest.mark.anyio
async def test_caller_memories_injected_into_prompt_sensitive_excluded():
    """Fetches top 3 memories, filters out sensitive ones, and injects into system prompt."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_memory_{int(time.time() * 1000)}"

    mock_memories = [
        {"id": 1, "text": "Prefers WFH on Fridays", "score": 0.9},
        {"id": 2, "text": "Has medical treatment for chronic illness", "score": 0.85},  # Sensitive!
        {"id": 3, "text": "Prefers morning meetings before 11 AM", "score": 0.8},
    ]

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Sure thing!"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=MagicMock(choices=[mock_choice]))
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.search_memory", return_value=mock_memories):
        async for _ in run_agent(caller=priya, session_id=session_id, user_message="Plan my Friday schedule"):
            pass

        sys_msg = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "Known preferences of this employee:" in sys_msg
        assert "Prefers WFH on Fridays" in sys_msg
        assert "Prefers morning meetings before 11 AM" in sys_msg
        # Sensitive medical/illness preference was completely excluded!
        assert "chronic illness" not in sys_msg
        assert "medical treatment" not in sys_msg


def test_sensitive_memory_extended_patterns():
    """Verifies extended sensitive patterns: health, illness, pregnancy, visa,

    religion, caste, sexual orientation, lawsuit, disability, mental.
    """
    assert is_sensitive_memory("Employee has chronic health issues") is True
    assert is_sensitive_memory("Employee reported illness last week") is True
    assert is_sensitive_memory("Employee announced pregnancy") is True
    assert is_sensitive_memory("Requires H1B visa sponsorship") is True
    assert is_sensitive_memory("Follows Hindu religion") is True
    assert is_sensitive_memory("Belongs to SC/ST caste") is True
    assert is_sensitive_memory("Disclosed sexual orientation") is True
    assert is_sensitive_memory("Involved in lawsuit with prior employer") is True
    assert is_sensitive_memory("Requires disability accommodations") is True
    assert is_sensitive_memory("Taking mental health sabbatical") is True
    # Non-sensitive preference
    assert is_sensitive_memory("Prefers working from home on Fridays") is False
    assert is_sensitive_memory("Prefers Slack direct message over email") is False


# ==============================================================================
# 5. NON-BLOCKING CONCURRENCY & DATES
# ==============================================================================
@pytest.mark.anyio
async def test_concurrent_chat_requests_overlap():
    """Verifies that 2 concurrent /chat calls with a slow mocked tool overlap

    instead of running one after the other.
    """
    priya = resolve_caller("ACM-008")

    # Mock tool call from model
    mock_tc = MagicMock()
    mock_tc.id = "call_slow_tool"
    mock_tc.function.name = "get_upcoming_holidays"
    mock_tc.function.arguments = "{}"

    mock_tool_resp = MagicMock()
    mock_tool_resp.choices = [MagicMock(message=MagicMock(tool_calls=[mock_tc], content=None), finish_reason="tool_calls")]

    mock_no_tool_resp = MagicMock()
    mock_no_tool_resp.choices = [MagicMock(message=MagicMock(tool_calls=None, content="All set"), finish_reason="stop")]

    async def mock_stream_resp():
        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=MagicMock(content="Holidays retrieved"), finish_reason="stop")]
        yield chunk

    # Each turn gets 1 tool call then finishes
    session_tool_called = set()

    async def custom_call_llm(**kwargs):
        if kwargs.get("stream"):
            return mock_stream_resp()
        msgs = kwargs.get("messages", [])
        # If this turn already executed the tool, finish
        has_tool_res = any(m.get("role") == "tool" for m in msgs)
        if has_tool_res:
            return mock_no_tool_resp
        return mock_tool_resp

    mock_client = AsyncMock()
    mock_client.chat.completions.create = custom_call_llm
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    tool_starts = []
    tool_ends = []

    # Slow tool: takes 0.3s in worker thread
    def slow_tool_impl(name, arguments, caller):
        t0 = time.time()
        tool_starts.append(t0)
        time.sleep(0.3)
        t1 = time.time()
        tool_ends.append(t1)
        return {"holidays": []}

    async def run_turn(idx: int):
        events = []
        async for ev in run_agent(caller=priya, session_id=f"concurrent_{idx}", user_message=f"Upcoming holidays {idx}"):
            events.append(ev)
        return events

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.get_chat_history", return_value=[]), \
         patch("app.agent.add_chat_message", return_value=1), \
         patch("app.agent.upsert_chat_session", return_value=None), \
         patch("app.agent.search_memory", return_value=[]), \
         patch("app.agent.search_policies", return_value=[]), \
         patch("app.agent.build_system_prompt", return_value="System prompt"), \
         patch("app.agent.execute_tool", side_effect=slow_tool_impl):
        start_time = time.time()
        res1, res2 = await asyncio.gather(run_turn(1), run_turn(2))
        duration = time.time() - start_time

    assert len(res1) > 0
    assert len(res2) > 0
    # Both tools ran
    assert len(tool_starts) == 2
    # The two tool executions in worker threads overlapped in time!
    # (i.e. second tool started before first tool finished)
    assert tool_starts[1] < tool_ends[0]
    # Total duration is close to 0.3s, strictly less than serial 2 * 0.3s = 0.6s
    assert duration < 0.55


# ==============================================================================
# 6. TOOL & DATE FIXES
# ==============================================================================
def test_extended_parse_date_range():
    """Tests extended parse_date_range formats: yesterday, last week, last month,

    next month, this year, month names, and last N days.
    """
    today = get_current_date()

    # Yesterday
    yest_s, yest_e = parse_date_range("yesterday", ref_date=today)
    assert yest_s == today - timedelta(days=1)
    assert yest_e == today - timedelta(days=1)

    # Last week
    lw_s, lw_e = parse_date_range("last week", ref_date=today)
    assert (lw_e - lw_s).days == 6
    assert lw_s.weekday() == 0  # Monday
    assert lw_e.weekday() == 6  # Sunday

    # Last month
    lm_s, lm_e = parse_date_range("last month", ref_date=today)
    assert lm_s.day == 1
    assert lm_e.day in (28, 29, 30, 31)

    # Next month
    nm_s, nm_e = parse_date_range("next month", ref_date=today)
    assert nm_s.day == 1
    assert nm_e.day in (28, 29, 30, 31)

    # This year
    ty_s, ty_e = parse_date_range("this year", ref_date=today)
    assert ty_s == date(today.year, 1, 1)
    assert ty_e == date(today.year, 12, 31)

    # Last N days
    l14_s, l14_e = parse_date_range("last 14 days", ref_date=today)
    assert l14_s == today - timedelta(days=14)
    assert l14_e == today

    # Month names
    sep_s, sep_e = parse_date_range("September", ref_date=today)
    assert sep_s == date(today.year, 9, 1)
    assert sep_e == date(today.year, 9, 30)

    sep26_s, sep26_e = parse_date_range("sep 2026", ref_date=today)
    assert sep26_s == date(2026, 9, 1)
    assert sep26_e == date(2026, 9, 30)

    # Unparseable format returns clear error
    with pytest.raises(ValueError, match="Unrecognized date range format"):
        parse_date_range("whenever convenient")


def test_cap_leave_requests_and_team_on_leave_default_20():
    """get_leave_requests and get_team_on_leave must cap results at limit (default 20)

    and tell how many were omitted.
    """
    priya = resolve_caller("ACM-008")
    db = get_session()
    try:
        # Seed 25 leave requests for Priya
        db.query(LeaveRequest).filter(LeaveRequest.employee_id == priya.employee_id).delete()
        for i in range(25):
            db.add(LeaveRequest(
                employee_id=priya.employee_id,
                leave_type="Casual",
                start_date=date(2026, 1, 1) + timedelta(days=i),
                end_date=date(2026, 1, 1) + timedelta(days=i),
                days=1,
                status="approved",
                reason=f"Trip {i}",
            ))
        db.commit()

        # Query leave requests with default limit (20)
        res = execute_tool("get_leave_requests", {}, caller=priya)
        assert res["total_records"] == 25
        assert len(res["requests"]) == 20
        assert res["omitted"] == 5
        assert "5 records omitted (capped at 20)" in res["omission_notice"]

        # Test manager team on leave capping
        rahul = resolve_caller("ACM-005")
        team_res = execute_tool(
            "get_team_on_leave",
            {"date_range": "2026-01-01 to 2026-01-30", "limit": 10},
            caller=rahul,
        )
        assert team_res["total_records"] >= 20
        assert len(team_res["leaves"]) == 10
        assert team_res["omitted"] == team_res["total_records"] - 10
        assert "records omitted (capped at 10)" in team_res["omission_notice"]

    finally:
        db.close()


# ==============================================================================
# 7. LATENCY OPTIMIZATION & FAST PATHS
# ==============================================================================
def test_route_intent_deterministic_fast_paths():
    """Verifies that route_intent correctly maps common queries to fast-path tools."""
    from app.router import route_intent
    priya = resolve_caller("ACM-008")  # employee
    rahul = resolve_caller("ACM-005")  # manager
    anita = resolve_caller("ACM-002")  # hr_admin

    # 1. Leave balance
    assert route_intent("my leave balance", priya)["name"] == "get_leave_balance"
    assert route_intent("how many casual leaves do I have left?", priya)["name"] == "get_leave_balance"
    assert route_intent("check my leaves", priya)["name"] == "get_leave_balance"

    # 2. Upcoming holidays
    assert route_intent("upcoming holidays", priya)["name"] == "get_upcoming_holidays"
    assert route_intent("when is the next holiday", priya)["name"] == "get_upcoming_holidays"

    # 3. Payslip
    assert route_intent("my last payslip", priya)["name"] == "get_payslip"
    assert route_intent("show my payslip for august 2026", priya)["name"] == "get_payslip"

    # 4. Attendance
    assert route_intent("my attendance", priya)["name"] == "get_attendance_summary"
    att_last = route_intent("attendance last month", priya)
    assert att_last["name"] == "get_attendance_summary"
    assert "month" in att_last["arguments"]

    # 5. Manager / Profile
    assert route_intent("who is my manager", priya)["name"] == "get_employee_profile"
    assert route_intent("my profile", priya)["name"] == "get_employee_profile"

    # 6. Team on leave (Manager / HR only)
    assert route_intent("who is on leave this week", priya) is None  # Employee not fast-routed
    mgr_leave = route_intent("who is on leave this week", rahul)
    assert mgr_leave["name"] == "get_team_on_leave"
    assert mgr_leave["arguments"]["date_range"] == "this week"

    # 7. Headcount (HR Admin only)
    assert route_intent("company headcount", priya) is None
    assert route_intent("company headcount", rahul) is None
    assert route_intent("company headcount", anita)["name"] == "get_headcount_stats"

    # Policy questions must NOT be intercepted by data fast-paths
    assert route_intent("What is the leave policy?", priya) is None
    assert route_intent("What is the WFH policy?", priya) is None


@pytest.mark.anyio
async def test_rejected_reasoning_params_cached_per_model():
    """If Groq rejects reasoning parameters with an error, the model is cached in

    _REJECTED_REASONING_MODELS and subsequent calls omit reasoning params without retrying.
    """
    from app.agent import _REJECTED_REASONING_MODELS
    priya = resolve_caller("ACM-008")
    session_id = f"test_rej_reasoning_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Answer without reasoning."
    mock_resp = MagicMock(choices=[mock_choice])

    call_count = 0

    async def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if "reasoning_effort" in kwargs or "reasoning_format" in kwargs:
            raise GroqError("400: Unrecognized request argument 'reasoning_effort'")
        return mock_resp

    mock_client = AsyncMock()
    mock_client.chat.completions.create = mock_create
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="test-model")]))

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.get_settings") as mock_settings:
        s = get_settings().model_copy()
        s.groq_model = "test-model"
        mock_settings.return_value = s

        # First run: will hit 400 error, add 'test-model' to _REJECTED_REASONING_MODELS, and retry without reasoning
        events = []
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="General question 1"):
            events.append(ev)

        assert "test-model" in _REJECTED_REASONING_MODELS
        assert any(e["event"] == "done" for e in events)
        assert call_count == 2  # 1 failed with reasoning + 1 retry without reasoning

        # Second run with same model: should directly call WITHOUT reasoning kwargs, requiring only 1 call
        call_count = 0
        events_2 = []
        async for ev in run_agent(caller=priya, session_id=f"{session_id}_2", user_message="General question 2"):
            events_2.append(ev)

        assert any(e["event"] == "done" for e in events_2)
        assert call_count == 1  # Called directly without reasoning parameters!


@pytest.mark.anyio
async def test_stream_first_no_tool_calls_immediate_finish():
    """Unrecognized query streams with tools, and if no tool calls are made, finishes

    immediately without a second regeneration call.
    """
    priya = resolve_caller("ACM-008")
    session_id = f"test_stream_first_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.delta.tool_calls = None
    mock_choice.delta.content = "Hello there! How can I help you today?"

    mock_chunk = MagicMock(choices=[mock_choice])

    async def mock_stream(**kwargs):
        yield mock_chunk

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=mock_stream)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        tokens = []
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Hi"):
            if ev["event"] == "token":
                tokens.append(ev["data"]["token"])

        assert "".join(tokens) == "Hello there! How can I help you today?"
        # Made only 1 LLM call!
        assert mock_client.chat.completions.create.call_count == 1


def test_system_prompt_sentence_limit():
    """Verifies that the system prompt specifies answering in at most 6 sentences."""
    from app.agent import build_system_prompt
    priya = resolve_caller("ACM-008")
    prompt = build_system_prompt(priya)
    assert "Answer in at most 6 sentences unless the user asks for detail" in prompt


# ==============================================================================
# 11. HISTORY COUNT IN CONTEXT_INFO & PENDING SAVES RACE CONDITION
# ==============================================================================
@pytest.mark.anyio
async def test_brand_new_session_context_info_history_count_zero():
    """Brand-new session: context_info.history_count == 0 and history_limit == 6."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_brand_new_{int(time.time() * 1000)}"

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Welcome! How can I help you?"
    mock_resp = MagicMock(choices=[mock_choice])

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        context_info = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Hello"):
            if ev["event"] == "context_info":
                context_info = ev["data"]

        assert context_info is not None
        assert context_info["history_count"] == 0
        assert context_info["history_total"] == 0
        assert context_info["history_omitted"] == 0
        assert context_info["history_user"] == 0
        assert context_info["history_assistant"] == 0
        assert "history_limit" not in context_info


@pytest.mark.anyio
async def test_history_count_progression_uncapped():
    """History count grows by 2 every turn and is not capped at 6 (Turn 1: 0, Turn 2: 2, Turn 3: 4, Turn 4: 6, Turn 5: 8, Turn 6: 10, Turn 7: 12)."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_hist_progression_{int(time.time() * 1000)}"

    def make_mock_client(reply_text):
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message.tool_calls = None
        choice.message.content = reply_text
        resp = MagicMock(choices=[choice])
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=resp)
        client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))
        return client

    # Turn 1
    mock_client1 = make_mock_client("Reply 1")
    with patch("app.agent.get_groq_client", return_value=mock_client1):
        ctx1 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 1 msg"):
            if ev["event"] == "context_info":
                ctx1 = ev["data"]
        assert ctx1["history_count"] == 0

    # Turn 2
    mock_client2 = make_mock_client("Reply 2")
    with patch("app.agent.get_groq_client", return_value=mock_client2):
        ctx2 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 2 msg"):
            if ev["event"] == "context_info":
                ctx2 = ev["data"]
        assert ctx2["history_count"] == 2

    # Turn 3
    mock_client3 = make_mock_client("Reply 3")
    with patch("app.agent.get_groq_client", return_value=mock_client3):
        ctx3 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 3 msg"):
            if ev["event"] == "context_info":
                ctx3 = ev["data"]
        assert ctx3["history_count"] == 4

    # Turn 4
    mock_client4 = make_mock_client("Reply 4")
    with patch("app.agent.get_groq_client", return_value=mock_client4):
        ctx4 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 4 msg"):
            if ev["event"] == "context_info":
                ctx4 = ev["data"]
        assert ctx4["history_count"] == 6

    # Turn 5 (uncapped, reports 8)
    mock_client5 = make_mock_client("Reply 5")
    with patch("app.agent.get_groq_client", return_value=mock_client5):
        ctx5 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 5 msg"):
            if ev["event"] == "context_info":
                ctx5 = ev["data"]
        assert ctx5["history_count"] == 8

    # Turn 6 (after 5 completed turns, reports 10)
    mock_client6 = make_mock_client("Reply 6")
    with patch("app.agent.get_groq_client", return_value=mock_client6):
        ctx6 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 6 msg"):
            if ev["event"] == "context_info":
                ctx6 = ev["data"]
        assert ctx6["history_count"] == 10

    # Turn 7 (after 6 completed turns, reports 12)
    mock_client7 = make_mock_client("Reply 7")
    with patch("app.agent.get_groq_client", return_value=mock_client7):
        ctx7 = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 7 msg"):
            if ev["event"] == "context_info":
                ctx7 = ev["data"]
        assert ctx7["history_count"] == 12


@pytest.mark.anyio
async def test_immediate_follow_up_after_done_includes_previous_assistant_reply_with_delay():
    """Immediately sending a follow-up right after done still includes the previous assistant reply (slow DB write simulated with a delay)."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_pending_race_{int(time.time() * 1000)}"

    from app.agent import PENDING_SAVES
    import app.agent as agent_module

    mock_choice1 = MagicMock(finish_reason="stop", message=MagicMock(tool_calls=None, content="Assistant reply turn 1"))
    mock_resp1 = MagicMock(choices=[mock_choice1])
    mock_client1 = AsyncMock()
    mock_client1.chat.completions.create = AsyncMock(return_value=mock_resp1)
    mock_client1.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    mock_choice2 = MagicMock(finish_reason="stop", message=MagicMock(tool_calls=None, content="Assistant reply turn 2"))
    mock_resp2 = MagicMock(choices=[mock_choice2])
    mock_client2 = AsyncMock()
    mock_client2.chat.completions.create = AsyncMock(return_value=mock_resp2)
    mock_client2.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    orig_add_chat_message = agent_module.add_chat_message

    def slow_add_chat_message(*args, **kwargs):
        if kwargs.get("role") == "assistant":
            time.sleep(0.3)
        return orig_add_chat_message(*args, **kwargs)

    with patch("app.agent.get_groq_client", return_value=mock_client1), \
         patch("app.agent.add_chat_message", side_effect=slow_add_chat_message):

        gen1 = run_agent(caller=priya, session_id=session_id, user_message="User message turn 1")

        done_seen = False
        async for ev in gen1:
            if ev["event"] == "done":
                done_seen = True
            elif ev["event"] == "timing":
                break
        assert done_seen

        # At this exact moment, asst_save_task is in PENDING_SAVES and sleeping
        assert session_id in PENDING_SAVES
        assert not PENDING_SAVES[session_id].done()

        # Immediately launch turn 2 follow-up without awaiting remainder of gen1
        with patch("app.agent.get_groq_client", return_value=mock_client2):
            ctx2 = None
            async for ev in run_agent(caller=priya, session_id=session_id, user_message="User message turn 2"):
                if ev["event"] == "context_info":
                    ctx2 = ev["data"]

            # Turn 2 must have waited for turn 1 assistant save, so history includes both turn 1 msgs
            assert ctx2 is not None
            assert ctx2["history_count"] == 2

            # Check messages sent to model in turn 2 included turn 1 assistant reply
            call_kwargs = mock_client2.chat.completions.create.call_args.kwargs
            prompt_msgs = call_kwargs["messages"]
            prompt_contents = [m.get("content") for m in prompt_msgs]
            assert "Assistant reply turn 1" in prompt_contents

        # Finish exhausting gen1
        try:
            async for _ in gen1:
                pass
        except Exception:
            pass


@pytest.mark.anyio
async def test_failed_turns_are_not_counted_in_history_count():
    """Failed turns are not counted in context_info.history_count."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_failed_not_counted_{int(time.time() * 1000)}"

    # 1. Seed 1 successful completed turn
    add_chat_message(session_id, "user", "What is casual leave?", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "You get 12 casual leaves.", user_id=priya.emp_code, status="ok")

    # 2. Seed a failed assistant turn
    add_chat_message(session_id, "assistant", "", user_id=priya.emp_code, status="failed", error_code="GroqError")

    # 3. Next turn runs: verify context_info.history_count is 2, not 3 (failed turn not counted)
    mock_choice = MagicMock(finish_reason="stop", message=MagicMock(tool_calls=None, content="Next reply"))
    mock_resp = MagicMock(choices=[mock_choice])
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Next question"):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        assert ctx is not None
        assert ctx["history_count"] == 2

