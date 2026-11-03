"""Redis-backed answer cache — same interface as AnswerCache, shared across processes.

与内存版的差别：
  · 版本号与缓存都存 Redis → 多 worker 共享，一个进程失效全体立即失效
  · TTL 交给 Redis（SETEX）→ 惰性 + 定期删除，无需应用层比对时间
  · 淘汰交给 Redis maxmemory-policy（allkeys-lru）→ 删除应用层淘汰逻辑
  · AOF 持久化 → 重启不丢
"""
from __future__ import annotations

import json
import logging

import redis

from .answer_cache import AnswerCache, CacheEntry

logger = logging.getLogger(__name__)


class RedisAnswerCache(AnswerCache):
    def __init__(self, url: str, ttl: int = 300, max_entries: int = 1000,
                 prefix: str = "rag:acache:"):
        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._ttl = ttl
        self._max_entries = max_entries   # 仅展示用：真正的淘汰在 Redis 侧
        self._prefix = prefix
        self._ver_key = prefix + "collection_version"
        self._stats_hits = prefix + "stats:hits"
        self._stats_misses = prefix + "stats:misses"
        self._stats_inval = prefix + "stats:invalidations"

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _is_meta(self, k: str) -> bool:
        return k == self._ver_key or ":stats:" in k

    # ── 版本号：共享，所有 worker 一致 ──────────────────────
    @property
    def collection_version(self) -> int:
        try:
            v = self._redis.get(self._ver_key)
            return int(v) if v is not None else 0
        except redis.RedisError as e:
            logger.warning("redis cache version read failed: %s", e)
            return 0

    def bump_collection_version(self) -> int:
        try:
            new_v = int(self._redis.incr(self._ver_key))
            self._redis.incr(self._stats_inval)
            logger.info("answer cache (redis) invalidated: version=%d", new_v)
            return new_v
        except redis.RedisError as e:
            logger.warning("redis cache bump failed: %s", e)
            return -1

    # ── 读写：失败降级为未命中，不让缓存拖垮问答 ────────────
    def get(self, key: str) -> CacheEntry | None:
        try:
            raw = self._redis.get(self._key(key))
        except redis.RedisError as e:
            logger.warning("redis cache get failed: %s", e)
            return None
        if raw is None:
            try:
                self._redis.incr(self._stats_misses)
            except redis.RedisError:
                pass
            return None
        try:
            self._redis.incr(self._stats_hits)
        except redis.RedisError:
            pass
        d = json.loads(raw)
        return CacheEntry(
            answer=d["answer"],
            sources=d.get("sources") or [],
            verification=d.get("verification"),
            collection_version=d.get("collection_version", 0),
            created_at=d.get("created_at", 0.0),
            hit_count=d.get("hit_count", 0),
        )

    def put(self, key: str, entry: CacheEntry) -> None:
        payload = json.dumps({
            "answer": entry.answer,
            "sources": entry.sources,
            "verification": entry.verification,
            "collection_version": entry.collection_version,
            "created_at": entry.created_at,
            "hit_count": entry.hit_count,
        }, ensure_ascii=False)
        try:
            self._redis.set(self._key(key), payload, ex=self._ttl)
        except redis.RedisError as e:
            logger.warning("redis cache put failed: %s", e)

    # ── 清理 ────────────────────────────────────────────────
    def clear(self) -> int:
        n = 0
        for k in self._redis.scan_iter(match=self._prefix + "*", count=200):
            if self._is_meta(k):
                continue
            n += self._redis.delete(k)
        return n

    def invalidate_for_collection(self, collection_version: int) -> int:
        # versioned keys -> old entries unreachable; TTL reaps them.
        # no clear(): a full wipe would also nuke still-valid entries.
        logger.info("answer cache invalidate(v=%s): skip wipe (versioned keys + ttl)", collection_version)
        return 0

    # ── 统计 ────────────────────────────────────────────────
    @property
    def stats(self) -> dict:
        try:
            hits = int(self._redis.get(self._stats_hits) or 0)
            misses = int(self._redis.get(self._stats_misses) or 0)
            entries = sum(1 for k in self._redis.scan_iter(
                match=self._prefix + "*", count=200) if not self._is_meta(k))
        except redis.RedisError as e:
            logger.warning("redis cache stats failed: %s", e)
            hits = misses = entries = 0
        total = hits + misses
        return {
            "backend": "redis",
            "entries": entries,
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl,
            "hits": hits,
            "misses": misses,
            "hit_rate": hits / max(total, 1),
            "stale_evictions": int(self._redis.get(self._stats_inval) or 0),
            "collection_version": self.collection_version,
        }