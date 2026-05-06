import redis.asyncio as redis
import os

# Process-wide singleton. redis-py's Redis() wraps a connection pool that
# is meant to be shared across requests — each client instance has its own
# pool, so creating a fresh client per request (the previous behavior) paid
# DNS + TCP + AUTH + SELECT on every command's first redis touch and threw
# the warm pool away when the request ended. Caching the instance lets the
# pool reuse connections across the whole process.
_client = None


def get_redis_client():
    global _client
    if _client is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            _client = redis.from_url(redis_url, decode_responses=False)
        except Exception as e:
            print(f"ERROR: Failed to connect to Redis at {redis_url}")
            print(f"Exception: {e}")
            raise e
    return _client
