import hashlib
import threading
import time

from fastapi import Request

from app.core.config import settings
from app.core.errors import AppError
from app.core.locations import get_redis

_counts: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


class RateLimiter:
    """Fixed-window limiter. Redis when available, otherwise per-process memory."""

    def __init__(self, scope: str, limit: int, window_seconds: int = 60):
        self.scope, self.limit, self.window = scope, limit, window_seconds

    def __call__(self, request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        ident = request.client.host if request.client else "unknown"
        auth = request.headers.get("authorization")
        if auth:
            ident += ":" + hashlib.sha1(auth.encode()).hexdigest()[:12]
        if self._incr(f"rl:{self.scope}:{ident}") > self.limit:
            raise AppError("RATE_LIMITED", "Too many requests. Try again shortly.", 429)

    def _incr(self, key: str) -> int:
        r = get_redis()
        if r:
            try:
                n = r.incr(key)
                if n == 1:
                    r.expire(key, self.window)
                return int(n)
            except Exception:
                pass
        now = time.time()
        with _lock:
            n, reset = _counts.get(key, (0, now + self.window))
            if reset < now:
                n, reset = 0, now + self.window
            n += 1
            _counts[key] = (n, reset)
            return n
