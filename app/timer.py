import time
from typing import Any, Dict, Optional


class StageTimer:
    """Helper to record elapsed milliseconds for individual stages of a chat request."""

    def __init__(self) -> None:
        self.t_start = time.perf_counter()
        self.auth: float = 0.0
        self.session_owner_check: float = 0.0
        self.history_load: float = 0.0
        self.memory_fetch: float = 0.0
        self.system_prompt_build: float = 0.0
        self.policy_search: float = 0.0
        self.tool_calls: Dict[str, float] = {}
        self.tools_total: float = 0.0
        self.ttft: float = 0.0
        self.llm_total: float = 0.0
        self.db_writes: float = 0.0
        self.total_request: float = 0.0

    def record(self, stage: str, elapsed_ms: float) -> None:
        """Sets the elapsed milliseconds for a known stage."""
        if hasattr(self, stage):
            setattr(self, stage, round(elapsed_ms, 2))
        elif stage.startswith("tool_"):
            tool_name = stage[5:]
            self.tool_calls[tool_name] = round(elapsed_ms, 2)
            self.tools_total = round(sum(self.tool_calls.values()), 2)

    def record_tool(self, tool_name: str, elapsed_ms: float) -> None:
        self.tool_calls[tool_name] = round(elapsed_ms, 2)
        self.tools_total = round(sum(self.tool_calls.values()), 2)

    def finish(self) -> Dict[str, Any]:
        """Calculates total request duration and returns the timing dictionary."""
        self.total_request = round((time.perf_counter() - self.t_start) * 1000, 2)
        return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "auth": round(self.auth, 2),
            "session_owner_check": round(self.session_owner_check, 2),
            "history_load": round(self.history_load, 2),
            "memory_fetch": round(self.memory_fetch, 2),
            "system_prompt_build": round(self.system_prompt_build, 2),
            "policy_search": round(self.policy_search, 2),
            "tool_calls": self.tool_calls,
            "tools_total": round(self.tools_total, 2),
            "ttft": round(self.ttft, 2),
            "llm_total": round(self.llm_total, 2),
            "db_writes": round(self.db_writes, 2),
            "total_request": round(self.total_request, 2),
        }

    def summary_line(self, session_id: str = "") -> str:
        """Returns a single log-friendly line summarizing stage timings."""
        tools_str = f" tools={self.tools_total:.1f}ms" if self.tools_total else ""
        return (
            f"TIMING session={session_id} total={self.total_request:.1f}ms "
            f"auth={self.auth:.1f}ms owner={self.session_owner_check:.1f}ms "
            f"history={self.history_load:.1f}ms memory={self.memory_fetch:.1f}ms "
            f"prompt={self.system_prompt_build:.1f}ms policy={self.policy_search:.1f}ms"
            f"{tools_str} ttft={self.ttft:.1f}ms llm={self.llm_total:.1f}ms "
            f"db_writes={self.db_writes:.1f}ms"
        )
