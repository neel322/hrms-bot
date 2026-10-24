import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from app.agent import run_agent
from app.auth import resolve_caller
from app.db import init_db

init_db()

priya = resolve_caller("ACM-008")
session_id = f"verify_prog_{int(time.time() * 1000)}"


def make_mock_client():
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.tool_calls = None
    choice.message.content = "Assistant response."
    resp = MagicMock(choices=[choice])
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    client.models.list = AsyncMock(return_value=MagicMock(data=[MagicMock(id="qwen/qwen3.8-27b")]))
    return client


async def main():
    print("=" * 60, flush=True)
    print("RUNNING LIVE SESSION HISTORY PROGRESSION TEST", flush=True)
    print(f"Session ID: {session_id}", flush=True)
    print("=" * 60, flush=True)

    client = make_mock_client()

    with patch("app.agent.get_groq_client", return_value=client):
        for q_num in range(1, 8):
            user_msg = f"User Question #{q_num}"
            ctx_data = None
            async for ev in run_agent(caller=priya, session_id=session_id, user_message=user_msg):
                if ev["event"] == "context_info":
                    ctx_data = ev["data"]

            hist_count = ctx_data["history_count"]
            hist_total = ctx_data["history_total"]
            hist_u = ctx_data["history_user"]
            hist_a = ctx_data["history_assistant"]

            print(f"Query #{q_num}: 'History sent' = {hist_count} (Total in session: {hist_total}, User msgs: {hist_u}, Assistant msgs: {hist_a})", flush=True)

    print("=" * 60, flush=True)

if __name__ == "__main__":
    asyncio.run(main())
