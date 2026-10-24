import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.agent import PENDING_SAVES, run_agent, select_history_for_prompt
from app.auth import resolve_caller
from app.config import get_settings
from app.db import (
    ChatHistory,
    add_chat_message,
    delete_chat_session,
    get_chat_history,
    get_session,
    init_db,
)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()


def make_mock_client(reply_text="Default response"):
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.tool_calls = None
    choice.message.content = reply_text
    resp = MagicMock(choices=[choice])
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))
    return client


# --- 1. Unit Tests for select_history_for_prompt ---

def test_select_history_no_cap():
    """When HISTORY_MAX_MESSAGES is 0 and within char budget, full history is returned."""
    msgs = [
        {"role": "user", "content": f"Q{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"A{i}"}
        for i in range(10)
    ]
    with patch("app.agent.get_settings") as mock_settings:
        s = MagicMock()
        s.history_max_messages = 0
        s.history_max_chars = 24000
        mock_settings.return_value = s

        selected, omitted = select_history_for_prompt(msgs)
        assert len(selected) == 10
        assert omitted == 0
        assert selected == msgs


def test_select_history_max_messages_cap():
    """When HISTORY_MAX_MESSAGES is 8, only the newest 8 messages are kept."""
    msgs = [
        {"role": "user", "content": f"Q{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"A{i}"}
        for i in range(12)  # 6 pairs
    ]
    with patch("app.agent.get_settings") as mock_settings:
        s = MagicMock()
        s.history_max_messages = 8
        s.history_max_chars = 24000
        mock_settings.return_value = s

        selected, omitted = select_history_for_prompt(msgs)
        assert len(selected) == 8
        assert omitted == 4
        assert selected[0]["role"] == "user"
        assert selected == msgs[-8:]


def test_select_history_max_chars_drops_oldest_pairs_never_orphan_assistant():
    """When history exceeds HISTORY_MAX_CHARS, oldest user+assistant pairs are dropped."""
    # 3 pairs of messages, each message is 100 chars (pair = 200 chars)
    # Total chars = 600
    msgs = []
    for i in range(3):
        msgs.append({"role": "user", "content": f"User question {i}: " + ("x" * 80)})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}: " + ("y" * 80)})

    with patch("app.agent.get_settings") as mock_settings:
        s = MagicMock()
        s.history_max_messages = 0
        # Budget fits exactly 2 pairs (~400 chars), not all 3
        s.history_max_chars = 450
        mock_settings.return_value = s

        selected, omitted = select_history_for_prompt(msgs)
        # Should drop pair 0, keeping pairs 1 and 2
        assert len(selected) == 4
        assert omitted == 2
        assert selected[0]["content"].startswith("User question 1")
        assert selected[0]["role"] == "user"
        assert selected[1]["role"] == "assistant"
        assert selected[2]["role"] == "user"
        assert selected[3]["role"] == "assistant"


# --- 2. End-to-End Agent History Tests ---

@pytest.mark.anyio
async def test_fresh_session_history_count_zero():
    """Brand-new session context_info reports 0 history and all breakdown fields."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_fresh_{int(time.time() * 1000)}"

    mock_client = make_mock_client("Hello there!")
    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Initial hello"):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        assert ctx is not None
        assert ctx["history_count"] == 0
        assert ctx["history_total"] == 0
        assert ctx["history_omitted"] == 0
        assert ctx["history_user"] == 0
        assert ctx["history_assistant"] == 0
        assert "history_limit" not in ctx


@pytest.mark.anyio
async def test_uncapped_history_progression_up_to_60():
    """Session history grows past 6: after 5 pairs it reports 10, after 6 reports 12, after 30 reports 60."""
    priya = resolve_caller("ACM-008")

    # 1. After 5 completed pairs (10 messages), next query reports 10
    sid_5 = f"test_prog_5_{int(time.time() * 1000)}"
    for i in range(5):
        add_chat_message(sid_5, "user", f"Question {i+1}", user_id=priya.emp_code, status="ok")
        add_chat_message(sid_5, "assistant", f"Answer {i+1}", user_id=priya.emp_code, status="ok")

    mock_client = make_mock_client("Reply after 5 pairs")
    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=sid_5, user_message="Next question after 5"):
            if ev["event"] == "context_info":
                ctx = ev["data"]
        assert ctx["history_count"] == 10
        assert ctx["history_total"] == 10
        assert ctx["history_omitted"] == 0
        assert ctx["history_user"] == 5
        assert ctx["history_assistant"] == 5

    # 2. After 6 completed pairs (12 messages), next query reports 12
    sid_6 = f"test_prog_6_{int(time.time() * 1000)}"
    for i in range(6):
        add_chat_message(sid_6, "user", f"Question {i+1}", user_id=priya.emp_code, status="ok")
        add_chat_message(sid_6, "assistant", f"Answer {i+1}", user_id=priya.emp_code, status="ok")

    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=sid_6, user_message="Next question after 6"):
            if ev["event"] == "context_info":
                ctx = ev["data"]
        assert ctx["history_count"] == 12
        assert ctx["history_total"] == 12
        assert ctx["history_omitted"] == 0
        assert ctx["history_user"] == 6
        assert ctx["history_assistant"] == 6

    # 3. After 30 completed pairs (60 messages), next query reports 60
    sid_30 = f"test_prog_30_{int(time.time() * 1000)}"
    for i in range(30):
        add_chat_message(sid_30, "user", f"Question {i+1}", user_id=priya.emp_code, status="ok")
        add_chat_message(sid_30, "assistant", f"Answer {i+1}", user_id=priya.emp_code, status="ok")

    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=sid_30, user_message="Next question after 30"):
            if ev["event"] == "context_info":
                ctx = ev["data"]
        assert ctx["history_count"] == 60
        assert ctx["history_total"] == 60
        assert ctx["history_omitted"] == 0
        assert ctx["history_user"] == 30
        assert ctx["history_assistant"] == 30


@pytest.mark.anyio
async def test_history_max_messages_setting_caps_prompt():
    """When HISTORY_MAX_MESSAGES=8, only 8 messages are sent even if session has 16."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_cap8_{int(time.time() * 1000)}"

    for i in range(8):  # 8 pairs = 16 messages
        add_chat_message(session_id, "user", f"Question {i+1}", user_id=priya.emp_code, status="ok")
        add_chat_message(session_id, "assistant", f"Answer {i+1}", user_id=priya.emp_code, status="ok")

    mock_client = make_mock_client("Reply")
    with patch("app.agent.get_settings") as mock_settings, \
         patch("app.agent.get_groq_client", return_value=mock_client):
        s = MagicMock()
        s.history_max_messages = 8
        s.history_max_chars = 24000
        mock_settings.return_value = s

        ctx = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Query with cap"):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        assert ctx["history_count"] == 8
        assert ctx["history_total"] == 16
        assert ctx["history_omitted"] == 8
        assert ctx["history_user"] == 4
        assert ctx["history_assistant"] == 4


@pytest.mark.anyio
async def test_failed_turns_excluded():
    """Failed turns are never counted in history_count or history_total."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_failed_excl_{int(time.time() * 1000)}"

    add_chat_message(session_id, "user", "Query 1", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "Answer 1", user_id=priya.emp_code, status="ok")
    # Failed turn
    add_chat_message(session_id, "assistant", "[Error] (failed)", user_id=priya.emp_code, status="failed")

    mock_client = make_mock_client("Reply")
    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Query 2"):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        assert ctx["history_count"] == 2
        assert ctx["history_total"] == 2


@pytest.mark.anyio
async def test_retried_query_does_not_double_count():
    """A retried query does not count its own message as earlier history."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_retry_dedup_{int(time.time() * 1000)}"

    # 1 successful pair
    add_chat_message(session_id, "user", "First Q", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "First A", user_id=priya.emp_code, status="ok")

    # In retry, the user message was already recorded in DB
    add_chat_message(session_id, "user", "Second Q (failing)", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "", user_id=priya.emp_code, status="failed")

    mock_client = make_mock_client("Retried reply")
    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        async for ev in run_agent(caller=priya, session_id=session_id, user_message="Second Q (failing)", retry=True):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        # Should report history_count == 2 (First Q and First A), NOT 3
        assert ctx["history_count"] == 2
        assert ctx["history_total"] == 2


@pytest.mark.anyio
async def test_cross_user_session_isolation():
    """Querying another user's session returns 0 history messages."""
    priya = resolve_caller("ACM-008")
    rahul = resolve_caller("ACM-005")
    session_id = f"test_isolation_{int(time.time() * 1000)}"

    # Seed messages under Priya's emp_code
    add_chat_message(session_id, "user", "Priya's private question", user_id=priya.emp_code, status="ok")
    add_chat_message(session_id, "assistant", "Priya's private answer", user_id=priya.emp_code, status="ok")

    mock_client = make_mock_client("Rahul reply")
    with patch("app.agent.get_groq_client", return_value=mock_client):
        ctx = None
        # Rahul accesses the session ID
        async for ev in run_agent(caller=rahul, session_id=session_id, user_message="Rahul's question"):
            if ev["event"] == "context_info":
                ctx = ev["data"]

        # Rahul must see 0 history because Priya owns the messages
        assert ctx["history_count"] == 0
        assert ctx["history_total"] == 0


@pytest.mark.anyio
async def test_quick_followup_pending_saves():
    """Quick follow-up right after done includes previous assistant reply via PENDING_SAVES."""
    priya = resolve_caller("ACM-008")
    session_id = f"test_quick_pending_{int(time.time() * 1000)}"

    orig_add_chat = add_chat_message

    def delayed_add_chat(*args, **kwargs):
        if kwargs.get("role") == "assistant":
            time.sleep(0.2)
        return orig_add_chat(*args, **kwargs)

    mock_c1 = make_mock_client("Assistant reply 1")
    mock_c2 = make_mock_client("Assistant reply 2")

    with patch("app.agent.get_groq_client", return_value=mock_c1), \
         patch("app.agent.add_chat_message", side_effect=delayed_add_chat):

        gen1 = run_agent(caller=priya, session_id=session_id, user_message="Turn 1")
        async for ev in gen1:
            if ev["event"] == "timing":
                break

        assert session_id in PENDING_SAVES

        with patch("app.agent.get_groq_client", return_value=mock_c2):
            ctx2 = None
            async for ev in run_agent(caller=priya, session_id=session_id, user_message="Turn 2"):
                if ev["event"] == "context_info":
                    ctx2 = ev["data"]

            assert ctx2 is not None
            assert ctx2["history_count"] == 2
