import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agent import (
    is_question_message,
    should_extract_preferences,
    get_chat_history,
)
from app.auth import get_jwt_secret
from app.config import Settings
from app.db import AuthCredentials, get_session, init_db
from app.main import app


def create_test_token(employee_id: int = 4, token_version = None, emp_code: str = "ACM-008", role: str = "employee") -> str:
    secret = get_jwt_secret()
    if token_version is None:
        session = get_session()
        try:
            creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == employee_id).first()
            token_version = creds.token_version if creds else 1
        finally:
            session.close()
    payload = {
        "employee_id": employee_id,
        "emp_code": emp_code,
        "role": role,
        "token_version": token_version,
        "exp": datetime.utcnow() + timedelta(hours=8),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    init_db()


def test_config_cache_dir_alias():
    """Verifies that config accepts FAST_EMBED_CACHE_DIR, CACHE_DIR, and defaults to ./models."""
    # 1. Default when neither env var is present
    orig_fecd = os.environ.pop("FAST_EMBED_CACHE_DIR", None)
    orig_cd = os.environ.pop("CACHE_DIR", None)
    try:
        s_default = Settings()
        assert s_default.cache_dir == "./models"

        # 2. FAST_EMBED_CACHE_DIR alias
        os.environ["FAST_EMBED_CACHE_DIR"] = "/custom/fast_embed_path"
        s_fecd = Settings()
        assert s_fecd.cache_dir == "/custom/fast_embed_path"

        # 3. CACHE_DIR fallback
        os.environ.pop("FAST_EMBED_CACHE_DIR", None)
        os.environ["CACHE_DIR"] = "/custom/legacy_cache_path"
        s_cd = Settings()
        assert s_cd.cache_dir == "/custom/legacy_cache_path"
    finally:
        if orig_fecd is not None:
            os.environ["FAST_EMBED_CACHE_DIR"] = orig_fecd
        else:
            os.environ.pop("FAST_EMBED_CACHE_DIR", None)
        if orig_cd is not None:
            os.environ["CACHE_DIR"] = orig_cd
        else:
            os.environ.pop("CACHE_DIR", None)


def test_question_detection():
    """Verifies is_question_message correctly detects question stems and marks."""
    assert is_question_message("What is my casual leave balance?") is True
    assert is_question_message("When is payroll credited?") is True
    assert is_question_message("How many leaves do I have left?") is True
    assert is_question_message("Can you tell me about the leave policy?") is True
    assert is_question_message("Do I have remaining sick leaves?") is True
    assert is_question_message("Will I get paid this Friday?") is True
    assert is_question_message("Is remote work allowed?") is True
    assert is_question_message("Does Acme provide health insurance?") is True

    # Non-questions
    assert is_question_message("Please note that I prefer morning meetings.") is False
    assert is_question_message("I like to take my breaks at 1 PM.") is False
    assert is_question_message("Remember that I usually work from home on Fridays.") is False


def test_memory_preference_trigger_narrowing():
    """Verifies preference extraction narrowing:
    - Ordinary requests with 'remind' or 'remember' DO NOT fire.
    - Questions DO NOT fire.
    - Explicit preferences DO fire.
    """
    # 1. Ordinary operational requests that previously caused false positives
    assert should_extract_preferences("remind me when payroll is credited") is False
    assert should_extract_preferences("remind me to apply for leave tomorrow") is False
    assert should_extract_preferences("remember to approve my timesheet") is False
    assert should_extract_preferences("remember to email HR") is False

    # 2. Questions containing preference words must NOT fire
    assert should_extract_preferences("What do I usually prefer?") is False
    assert should_extract_preferences("Can you remind me when holidays start?") is False
    assert should_extract_preferences("Do you remember my favorite leave type?") is False
    assert should_extract_preferences("Where do I usually work from?") is False

    # 3. Legitimate durable preferences MUST fire
    assert should_extract_preferences("Remember that I prefer morning meetings.") is True
    assert should_extract_preferences("Please remember that my preferred shift is early morning.") is True
    assert should_extract_preferences("Please note that I usually work from home on Wednesdays.") is True
    assert should_extract_preferences("I like to start work at 9 AM.") is True
    assert should_extract_preferences("My favorite seating is desk B-14.") is True
    assert should_extract_preferences("My work style is focused and quiet.") is True
    assert should_extract_preferences("I prefer tea over coffee.") is True


@pytest.mark.anyio
async def test_agent_timeout_marks_failed_turn():
    """Verifies that an agent run timing out emits error event and records status='failed'."""
    session_id = f"sess_timeout_{int(datetime.utcnow().timestamp())}"
    token = create_test_token(employee_id=4, emp_code="ACM-008", role="employee")

    # Mock run_agent to simulate a 30s timeout hanging
    async def _hanging_agent(*args, **kwargs):
        await asyncio.sleep(5)  # Simulate hang
        yield {"event": "done", "data": {}}

    real_timeout = asyncio.timeout
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={"access_token": token}) as client:
        # Patch asyncio.timeout with a tiny limit (0.05s) to test timeout handling cleanly
        with patch("asyncio.timeout", side_effect=lambda s: real_timeout(0.05)):
            with patch("app.main.run_agent", side_effect=_hanging_agent):
                async with client.stream(
                    "POST",
                    "/chat",
                    json={"session_id": session_id, "message": "Trigger timeout test", "retry": False},
                ) as resp:
                    assert resp.status_code == 200
                    lines = [line async for line in resp.aiter_lines()]
                    content = "\n".join(lines)
                    assert "event: error" in content
                    assert "TIMEOUT" in content or "timed out" in content

    # Verify that in database, failed status was recorded in chat_history
    history = get_chat_history(session_id=session_id, limit=10, exclude_failed=False)
    failed_rows = [h for h in history if h.get("status") == "failed"]
    assert len(failed_rows) >= 1
    assert failed_rows[0].get("error_code") == "TIMEOUT"


@pytest.mark.anyio
async def test_agent_cancellation_marks_failed_turn():
    """Verifies that when client disconnects/cancels, assistant turn is marked failed with ClientDisconnected."""
    session_id = f"sess_cancel_{int(datetime.utcnow().timestamp())}"
    token = create_test_token(employee_id=4, emp_code="ACM-008", role="employee")

    async def _cancelling_agent(*args, **kwargs):
        raise asyncio.CancelledError()
        yield {"event": "done", "data": {}}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={"access_token": token}) as client:
        with patch("app.main.run_agent", side_effect=_cancelling_agent):
            try:
                async with client.stream(
                    "POST",
                    "/chat",
                    json={"session_id": session_id, "message": "Disconnect test", "retry": False},
                ) as resp:
                    async for _ in resp.aiter_lines():
                        pass
            except Exception:
                pass

    history = get_chat_history(session_id=session_id, limit=10, exclude_failed=False)
    failed_rows = [h for h in history if h.get("status") == "failed"]
    assert len(failed_rows) >= 1
    assert failed_rows[0].get("error_code") == "ClientDisconnected"
