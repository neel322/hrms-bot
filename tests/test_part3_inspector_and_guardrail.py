import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import logging

from app.agent import (
    has_data_intent,
    normalize_tool_status,
    is_policy_question,
    run_agent,
)
from app.auth import resolve_caller
from app.config import get_settings


def test_has_data_intent_matches_keywords():
    data_queries = [
        "How much leave balance do I have left?",
        "Show my salary payslip for this month",
        "Check my attendance for September",
        "What upcoming holidays do we have?",
        "Who is on leave in my team?",
        "What is the total headcount by department?",
        "Who is my manager?",
        "Show employee profile for Priya",
    ]
    for q in data_queries:
        assert has_data_intent(q) is True, f"Expected data intent for: {q}"

    non_data_queries = [
        "hi",
        "hello there",
        "what is the company dress code?",
        "can you summarize the POSH harassment policy?",
        "thank you very much",
    ]
    for q in non_data_queries:
        assert has_data_intent(q) is False or is_policy_question(q), f"Query should be policy or non-data: {q}"


def test_normalize_tool_status():
    # Success
    s, sum_txt = normalize_tool_status({"leave_balance": 10, "summary": "Retrieved balances"})
    assert s == "success"
    assert sum_txt == "Retrieved balances"

    # Empty
    s, sum_txt = normalize_tool_status({"empty": True, "reason": "No attendance records logged."})
    assert s == "empty"
    assert sum_txt == "No attendance records logged."

    s, sum_txt = normalize_tool_status({"status": "empty", "message": "No records found"})
    assert s == "empty"
    assert sum_txt == "No records found"

    # Denied
    s, sum_txt = normalize_tool_status({"access_denied": True, "error": "Only HR admins can access organizational headcount stats."})
    assert s == "denied"
    assert "HR admins" in sum_txt

    s, sum_txt = normalize_tool_status({"status": "access_denied", "reason": "Restricted"})
    assert s == "denied"

    s, sum_txt = normalize_tool_status({"status": "denied", "reason": "Not permitted"})
    assert s == "denied"

    # Error
    s, sum_txt = normalize_tool_status({"error": "Failed to connect to database"})
    assert s == "error"
    assert "database" in sum_txt

    s, sum_txt = normalize_tool_status({"status": "error", "reason": "Execution failed"})
    assert s == "error"

    s, sum_txt = normalize_tool_status({"status": "failed", "reason": "Timeout"})
    assert s == "error"


@pytest.mark.anyio
async def test_guardrail_sets_tool_choice_required_for_data_queries():
    caller = resolve_caller("ACM-008")  # Priya (employee)
    session_id = "test_guardrail_data_1"

    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Here is the response."
    mock_resp.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    # Query matching data keyword that has NO deterministic fast-path (e.g. "complex leave query")
    data_query = "Please analyze my balance and leave patterns across departments"

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.route_intent", return_value=None):
        events = []
        async for event in run_agent(
            caller=caller,
            session_id=session_id,
            user_message=data_query,
        ):
            events.append(event)

        # Verify chat.completions.create was called with tool_choice="required"
        create_calls = mock_client.chat.completions.create.call_args_list
        assert len(create_calls) > 0
        first_call_kwargs = create_calls[0].kwargs
        assert first_call_kwargs.get("tool_choice") == "required"


@pytest.mark.anyio
async def test_guardrail_leaves_tool_choice_auto_for_greetings():
    caller = resolve_caller("ACM-008")
    session_id = "test_guardrail_greeting_1"

    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "Hello! How can I help you today?"
    mock_resp.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.route_intent", return_value=None):
        events = []
        async for event in run_agent(
            caller=caller,
            session_id=session_id,
            user_message="Hello there, good morning!",
        ):
            events.append(event)

        create_calls = mock_client.chat.completions.create.call_args_list
        assert len(create_calls) > 0
        first_call_kwargs = create_calls[0].kwargs
        assert first_call_kwargs.get("tool_choice") == "auto"


@pytest.mark.anyio
async def test_data_turn_finishing_without_tool_logs_warning(caplog):
    caller = resolve_caller("ACM-008")
    session_id = "test_guardrail_warning_1"

    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.tool_calls = None
    mock_choice.message.content = "No tool used reply."
    mock_resp.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))

    with patch("app.agent.get_groq_client", return_value=mock_client), \
         patch("app.agent.route_intent", return_value=None), \
         caplog.at_level(logging.WARNING, logger="hrms-bot.agent"):
        events = []
        async for event in run_agent(
            caller=caller,
            session_id=session_id,
            user_message="Show me my salary breakdown",
        ):
            events.append(event)

        # Must log a warning for data turn without tool result
        warning_found = any("Data-intent turn finished without any tool result" in r.message for r in caplog.records)
        assert warning_found is True

        done_event = next(e for e in events if e["event"] == "done")
        assert done_event["data"]["is_data"] is True
