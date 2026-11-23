"""Latest volunteer location: Redis when configured, in-process dict otherwise.
Only the latest point is kept hot; periodic snapshots are persisted to PostgreSQL."""
import json
import threading
import time

from app.core.config import settings

_mem: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
_redis = None
_redis_checked = False
TTL_SECONDS = 15 * 60


def get_redis():
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    if settings.redis_url:
        try:
            import redis

            client = redis.Redis.from_url(settings.redis_url, socket_timeout=1, decode_responses=True)
            client.ping()
            _redis = client
        except Exception:
            _redis = None
    return _redis


def set_latest(volunteer_id: str, lat: float, lon: float) -> None:
    payload = json.dumps({"lat": lat, "lon": lon, "ts": time.time()})
    r = get_redis()
    if r:
        try:
            r.set(f"vloc:{volunteer_id}", payload, ex=TTL_SECONDS)
            return
        except Exception:
            pass
    with _lock:
        _mem[volunteer_id] = (time.time() + TTL_SECONDS, payload)


def get_latest(volunteer_id: str) -> dict | None:
    r = get_redis()
    if r:
        try:
            raw = r.get(f"vloc:{volunteer_id}")
            return json.loads(raw) if raw else None
        except Exception:
            pass
    with _lock:
        item = _mem.get(volunteer_id)
    if not item or item[0] < time.time():
        return None
    return json.loads(item[1])
