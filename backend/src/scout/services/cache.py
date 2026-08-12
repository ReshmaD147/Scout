import time
import functools
import hashlib
import json

# Simple in-memory TTL cache — intentionally NOT a distributed cache
# (Redis, etc.) since this is a single-process demo app. Keyed on a hash
# of the actual arguments, so different queries never collide, and
# results expire automatically after TTL_SECONDS so stock/promotion
# changes are never stale for long. This caches EXPENSIVE DATA LOOKUPS
# only — never the final AI reply, since conversational context and
# phrasing genuinely need to be handled fresh every time.

TTL_SECONDS = 30
_cache: dict[str, tuple[float, object]] = {}


def _make_key(func_name: str, args: tuple, kwargs: dict) -> str:
    """Builds a stable cache key from a function's actual arguments.
    Skips the `session` argument (first positional arg in every function
    this decorates) since a DB session object isn't meaningfully
    hashable/comparable and isn't part of what makes two calls "the
    same query" anyway."""
    hashable_args = args[1:] if args else args
    raw = json.dumps([func_name, hashable_args, kwargs], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def ttl_cache(func):
    """Caches a function's return value for TTL_SECONDS, keyed on its
    arguments (excluding the DB session). Safe for read-only data lookups
    where a short staleness window is acceptable — NOT for anything that
    writes data, and NOT for final AI-generated replies."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        key = _make_key(func.__name__, args, kwargs)
        now = time.time()

        if key in _cache:
            cached_at, cached_value = _cache[key]
            if now - cached_at < TTL_SECONDS:
                return cached_value

        result = func(*args, **kwargs)
        _cache[key] = (now, result)
        return result

    return wrapper


def clear_cache():
    """Manual cache clear — useful after seeding/updating data during
    development, or could be called after an order/stock change in a
    more complete implementation."""
    _cache.clear()
