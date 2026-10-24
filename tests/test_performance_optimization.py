import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import get_jwt_secret, invalidate_auth_cache, _AUTH_VERIFY_CACHE
from app.db import (
    AuthCredentials,
    Employee,
    get_session,
    init_db,
    ping_database_keepalive,
    warmup_database_pool,
)
from app.main import app
from app.memory import (
    get_user_memories_cached,
    invalidate_user_memories_cache,
    _USER_MEMORIES_CACHE,
)
from app.timer import StageTimer
from app.vectorstore import clear_policy_cache, _POLICY_SEARCH_CACHE


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


@pytest.mark.anyio
async def test_x_response_time_header():
    """Verifies X-Response-Time header is injected by middleware."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert "x-response-time" in resp.headers
        assert resp.headers["x-response-time"].endswith("ms")


def test_db_pool_warmup_and_keepalive():
    """Verifies connection pool warmup and keepalive ping execute cleanly."""
    warmup_database_pool(n=3)
    ok = ping_database_keepalive()
    assert ok is True


def test_auth_and_memory_caching():
    """Verifies in-memory cache population and invalidation."""
    # Test memory caching
    _USER_MEMORIES_CACHE.clear()
    mems = get_user_memories_cached("ACM-008", limit=5)
    assert isinstance(mems, list)
    assert "acm-008" in _USER_MEMORIES_CACHE

    invalidate_user_memories_cache("ACM-008")
    assert "acm-008" not in _USER_MEMORIES_CACHE

    # Test policy cache
    _POLICY_SEARCH_CACHE[("test query", 4)] = ([], time.perf_counter() + 600)
    assert len(_POLICY_SEARCH_CACHE) > 0
    clear_policy_cache()
    assert len(_POLICY_SEARCH_CACHE) == 0

    # Test auth verify cache
    _AUTH_VERIFY_CACHE[1] = (MagicMock(), 1, False, time.perf_counter() + 30)
    assert 1 in _AUTH_VERIFY_CACHE
    invalidate_auth_cache(1)
    assert 1 not in _AUTH_VERIFY_CACHE


def test_stage_timer_and_timing_sse_event():
    """Verifies that StageTimer correctly tracks all stages and yields timing SSE event."""
    timer = StageTimer()
    timer.record("auth", 2.5)
    timer.record("session_owner_check", 3.0)
    timer.record("history_load", 1.5)
    timer.record("memory_fetch", 0.5)
    timer.record("system_prompt_build", 0.8)
    timer.record_tool("get_employee_profile", 45.0)
    timer.record("ttft", 250.0)
    timer.record("llm_total", 800.0)
    timer.record("db_writes", 4.0)

    result = timer.finish()
    assert result["auth"] == 2.5
    assert result["tools_total"] == 45.0
    assert result["ttft"] == 250.0
    assert result["total_request"] >= 0

    summary = timer.summary_line("sess-123")
    assert "TIMING session=sess-123" in summary
    assert "ttft=250.0ms" in summary


@pytest.mark.anyio
async def test_chat_emits_timing_sse_event():
    """Verifies that /chat yields timing SSE event with expected latency breakdown schema."""
    token = create_test_token(4)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={"access_token": token}) as client:
        session_id = f"timing_test_{int(time.time() * 1000)}"

        # Mock Groq stream
        mock_chunk = MagicMock()
        mock_choice = MagicMock()
        mock_choice.delta.content = "Here is your profile information."
        mock_choice.delta.tool_calls = None
        mock_chunk.choices = [mock_choice]

        async def mock_stream(*args, **kwargs):
            yield mock_chunk

        with patch("app.agent.get_groq_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())
            mock_get_client.return_value = mock_client

            async with client.stream(
                "POST",
                "/chat",
                json={"session_id": session_id, "message": "Who is my manager?"},
            ) as response:
                assert response.status_code == 200
                events = []
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        events.append(line[7:].strip())

                assert "done" in events
                assert "timing" in events


@pytest.mark.anyio
async def test_concurrency_requests_overlap():
    """Tests that two requests run concurrently and overlap rather than executing serially."""
    token = create_test_token(4)
    transport = ASGITransport(app=app)

    # Mock an LLM call that takes 0.3s during streaming
    mock_chunk = MagicMock()
    mock_choice = MagicMock()
    mock_choice.delta.content = "Done."
    mock_choice.delta.tool_calls = None
    mock_chunk.choices = [mock_choice]

    class DelayedStream:
        def __init__(self):
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            await asyncio.sleep(0.3)
            return mock_chunk

    # Prewarm caches for ACM-004 so pre-LLM phase is instant
    _USER_MEMORIES_CACHE["acm-004"] = ([], time.time())
    from app.agent import _PROMPT_INFO_CACHE
    _PROMPT_INFO_CACHE[4] = ({}, time.time())

    with patch("app.main.get_session_owner", return_value=None), \
         patch("app.main.get_chat_history", return_value=[]), \
         patch("app.main.add_chat_message", return_value=1), \
         patch("app.main.upsert_chat_session", return_value=None), \
         patch("app.agent.add_chat_message", return_value=1), \
         patch("app.agent.get_groq_client") as mock_get_client, \
         patch("app.agent.get_active_model", AsyncMock(return_value="qwen/qwen3.8-27b")):
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=lambda *a, **kw: DelayedStream())
        mock_client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))
        mock_get_client.return_value = mock_client

        async with AsyncClient(transport=transport, base_url="http://test", cookies={"access_token": token}) as client:
            t0 = time.perf_counter()
            req1 = client.post("/chat", json={"session_id": f"sess_c1_{int(time.time() * 1000)}", "message": "Hi 1"})
            req2 = client.post("/chat", json={"session_id": f"sess_c2_{int(time.time() * 1000) + 1}", "message": "Hi 2"})

            resp1, resp2 = await asyncio.gather(req1, req2)
            total_duration = time.perf_counter() - t0

            assert resp1.status_code == 200
            assert resp2.status_code == 200
            # If serial, 0.3 + 0.3 = 0.6s minimum. If concurrent, both overlap around ~0.35s (< 0.55s)
            assert total_duration < 0.55, f"Requests executed serially instead of concurrently ({total_duration:.2f}s)"
