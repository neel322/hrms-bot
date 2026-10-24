import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.db import (
    get_session,
    Employee,
    ChatSession,
    ChatHistory,
    AuthCredentials,
    upsert_chat_session,
    add_chat_message,
    delete_chat_session,
    init_db,
)
import jwt
from app.auth import get_jwt_secret


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    init_db()


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


def test_sessions_endpoint_returns_only_caller_sessions_newest_first(client):
    """GET /sessions only returns the caller's sessions, ordered newest first."""
    priya_cookie = get_cookie_for("ACM-008")  # Priya
    rahul_cookie = get_cookie_for("ACM-005")  # Rahul

    # Seed sessions for Priya
    s1_id = "priya_sess_old"
    s2_id = "priya_sess_new"
    s_rahul = "rahul_sess_1"

    # Clean up any existing
    delete_chat_session(s1_id)
    delete_chat_session(s2_id)
    delete_chat_session(s_rahul)

    now = datetime.utcnow()
    db = get_session()
    try:
        db.query(ChatSession).filter(ChatSession.user_id == "ACM-008").delete()
        db.commit()
    finally:
        db.close()

    db = get_session()
    try:
        # Priya older session
        db.add(ChatSession(
            session_id=s1_id,
            user_id="ACM-008",
            title="Priya Old Chat",
            created_at=now - timedelta(hours=2),
            last_message_at=now - timedelta(hours=2),
        ))
        # Priya newer session
        db.add(ChatSession(
            session_id=s2_id,
            user_id="ACM-008",
            title="Priya New Chat",
            created_at=now - timedelta(minutes=10),
            last_message_at=now - timedelta(minutes=10),
        ))
        # Rahul session
        db.add(ChatSession(
            session_id=s_rahul,
            user_id="ACM-005",
            title="Rahul Chat",
            created_at=now,
            last_message_at=now,
        ))
        db.commit()
    finally:
        db.close()

    # Priya fetches /sessions
    res_priya = client.get("/sessions?limit=100", cookies=priya_cookie)
    assert res_priya.status_code == 200
    priya_sessions = res_priya.json()
    priya_ids = [s["session_id"] for s in priya_sessions]

    # Must contain Priya's sessions, but NOT Rahul's
    assert s2_id in priya_ids
    assert s1_id in priya_ids
    assert s_rahul not in priya_ids

    # Must be ordered newest first
    idx_new = priya_ids.index(s2_id)
    idx_old = priya_ids.index(s1_id)
    assert idx_new < idx_old

    # Rahul fetches /sessions
    res_rahul = client.get("/sessions", cookies=rahul_cookie)
    assert res_rahul.status_code == 200
    rahul_sessions = res_rahul.json()
    rahul_ids = [s["session_id"] for s in rahul_sessions]
    assert s_rahul in rahul_ids
    assert s1_id not in rahul_ids
    assert s2_id not in rahul_ids

    # Clean up
    delete_chat_session(s1_id)
    delete_chat_session(s2_id)
    delete_chat_session(s_rahul)


def test_deleting_another_user_session_is_403(client):
    """DELETE /sessions/{id} returns 403 when trying to delete someone else's session."""
    priya_cookie = get_cookie_for("ACM-008")
    rahul_cookie = get_cookie_for("ACM-005")

    priya_sid = "priya_protected_sess"
    delete_chat_session(priya_sid)

    db = get_session()
    try:
        db.add(ChatSession(
            session_id=priya_sid,
            user_id="ACM-008",
            title="Priya Confidential Conversation",
        ))
        db.add(ChatHistory(
            session_id=priya_sid,
            user_id="ACM-008",
            role="user",
            content="Hello from Priya",
        ))
        db.commit()
    finally:
        db.close()

    # Rahul attempts to DELETE Priya's session -> 403 Forbidden
    res_del_forbidden = client.delete(f"/sessions/{priya_sid}", cookies=rahul_cookie)
    assert res_del_forbidden.status_code == 403
    assert "Forbidden" in res_del_forbidden.json()["detail"]

    # Priya deletes her own session -> 200 OK
    res_del_ok = client.delete(f"/sessions/{priya_sid}", cookies=priya_cookie)
    assert res_del_ok.status_code == 200
    assert res_del_ok.json()["status"] == "deleted"

    # Confirm it is gone
    db = get_session()
    try:
        sess = db.query(ChatSession).filter(ChatSession.session_id == priya_sid).first()
        msgs = db.query(ChatHistory).filter(ChatHistory.session_id == priya_sid).all()
        assert sess is None
        assert len(msgs) == 0
    finally:
        db.close()


def test_history_returns_messages_in_order_excluding_failed_turns(client):
    """GET /history for a session returns messages oldest to newest, excluding failed turns."""
    priya_cookie = get_cookie_for("ACM-008")
    sid = "priya_ordered_history_sess"
    delete_chat_session(sid)

    db = get_session()
    try:
        db.add(ChatSession(
            session_id=sid,
            user_id="ACM-008",
            title="Ordered History Test",
        ))
        # Msg 1: User
        db.add(ChatHistory(
            session_id=sid,
            user_id="ACM-008",
            role="user",
            content="First question",
        ))
        # Msg 2: Assistant (success)
        db.add(ChatHistory(
            session_id=sid,
            user_id="ACM-008",
            role="assistant",
            content="First answer",
        ))
        # Msg 3: User
        db.add(ChatHistory(
            session_id=sid,
            user_id="ACM-008",
            role="user",
            content="Second question",
        ))
        # Msg 4: Assistant (failed turn)
        db.add(ChatHistory(
            session_id=sid,
            user_id="ACM-008",
            role="assistant",
            content="[Error: Groq rate limit] (failed)",
        ))
        # Msg 5: Assistant (retried success answer)
        db.add(ChatHistory(
            session_id=sid,
            user_id="ACM-008",
            role="assistant",
            content="Second answer recovered",
        ))
        db.commit()
    finally:
        db.close()

    res = client.get(f"/history?session_id={sid}", cookies=priya_cookie)
    assert res.status_code == 200
    messages = res.json()

    # The failed turn must be excluded
    contents = [m["content"] for m in messages]
    assert "[Error: Groq rate limit] (failed)" not in contents

    # Must contain the non-failed messages in chronological order (oldest to newest)
    assert contents == ["First question", "First answer", "Second question", "Second answer recovered"]

    delete_chat_session(sid)


def test_retry_turn_removes_failed_assistant_and_avoids_duplicate_user_msg():
    """Verify backend logic for retry: removes failed assistant turn and does not duplicate user message."""
    import asyncio
    from app.agent import run_agent
    from app.auth import resolve_caller

    caller = resolve_caller("ACM-008")
    sid = "retry_test_session_123"
    delete_chat_session(sid)

    db = get_session()
    try:
        # Seed user question and failed assistant turn
        db.add(ChatSession(session_id=sid, user_id="ACM-008", title="Retry query test"))
        db.add(ChatHistory(session_id=sid, user_id="ACM-008", role="user", content="Test query for retry"))
        db.add(ChatHistory(session_id=sid, user_id="ACM-008", role="assistant", content="[Error: Test failure] (failed)"))
        db.commit()
    finally:
        db.close()

    # Verify initial state: 1 user, 1 failed assistant
    db = get_session()
    try:
        msgs = db.query(ChatHistory).filter(ChatHistory.session_id == sid).order_by(ChatHistory.id.asc()).all()
        assert len(msgs) == 2
        assert msgs[1].content == "[Error: Test failure] (failed)"
    finally:
        db.close()

    # Now execute run_agent with retry=True
    async def run_one():
        events = []
        async for event in run_agent(
            caller=caller,
            session_id=sid,
            user_message="Test query for retry",
            retry=True,
        ):
            events.append(event)
        return events

    asyncio.run(run_one())

    # Check database:
    # 1. The [Error: Test failure] (failed) must be gone!
    # 2. There should NOT be two "Test query for retry" user messages!
    db = get_session()
    try:
        user_msgs = db.query(ChatHistory).filter(
            ChatHistory.session_id == sid,
            ChatHistory.role == "user",
        ).all()
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "Test query for retry"

        failed_msgs = db.query(ChatHistory).filter(
            ChatHistory.session_id == sid,
            ChatHistory.content.like("%(failed)%"),
        ).all()
        assert len(failed_msgs) == 0
    finally:
        db.close()

    delete_chat_session(sid)

