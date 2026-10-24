import time
from collections import defaultdict
from typing import Dict, List
from fastapi import HTTPException, Request


class SimpleRateLimiter:
    """Sliding-window in-memory rate limiter per key (IP or user ID)."""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.history: Dict[str, List[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        timestamps = [t for t in self.history[key] if t > cutoff]
        if len(timestamps) >= self.max_requests:
            self.history[key] = timestamps
            return False
        timestamps.append(now)
        self.history[key] = timestamps
        return True

    def enforce(self, key: str, detail: str = "Rate limit exceeded. Please try again later.") -> None:
        if not self.check(key):
            raise HTTPException(status_code=429, detail=detail)

    def reset(self) -> None:
        """Clears all recorded request timestamps."""
        self.history.clear()


def get_client_ip(request: Request) -> str:
    """Extracts client IP from request, taking into account X-Forwarded-For if available."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown_ip"


# Standard limiters
login_ip_limiter = SimpleRateLimiter(max_requests=60, window_seconds=60)
chat_ip_limiter = SimpleRateLimiter(max_requests=60, window_seconds=60)
chat_user_limiter = SimpleRateLimiter(max_requests=60, window_seconds=60)
demo_login_ip_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)
activate_ip_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)


def reset_all_limiters() -> None:
    """Resets all active rate limiters."""
    login_ip_limiter.reset()
    chat_ip_limiter.reset()
    chat_user_limiter.reset()
    demo_login_ip_limiter.reset()
    activate_ip_limiter.reset()
