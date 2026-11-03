"""共享 rate limiter 实例，避免 main.py ↔ api/*.py 循环导入。"""

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address

from config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
    storage_uri=settings.redis_url or "memory://",
    in_memory_fallback_enabled=True,  # Redis down -> in-memory fallback (fail-open)
)

# surface slowapi fallback/recover logs into docker logs
_slowapi_log = logging.getLogger("slowapi")
_slowapi_log.addHandler(logging.StreamHandler())
_slowapi_log.setLevel(logging.INFO)
