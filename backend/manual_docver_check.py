"""手动验证：按文档维度的缓存失效。

验证五点：
  1. 同一批 sources 算出的 token 稳定
  2. 依赖不同文档的 token 不同
  3. bump docA 后，引用 docA 的 token 变
  4. bump docA 后，只引用 docB 的 token 不变   ← 核心收益
  5. sources 里没有 doc_id 时，退回全局版本兜底

不需要 pydantic / 完整依赖：用一个假的 config 模块顶掉 `from config import settings`。
只需要本地有 Redis（docker run -d -p 6379:6379 redis）。

跑法（在 backend 目录下）：
    python manual_docver_check.py
"""
import sys
import types

# ── 顶掉 config，避免拉起 pydantic 及整条配置链 ──────────────
_stub = types.ModuleType("config")
_stub.settings = types.SimpleNamespace(
    grounding_min_coverage=0.5,
    grounding_enforcement="strict",
    grounding_auto_cite_min_score=0.5,
    grounding_auto_cite_min_margin=0.1,
    redis_url="redis://localhost:6379/0",
    rag_answer_cache_ttl_seconds=60,
    rag_answer_cache_max_entries=100,
    rag_answer_cache_enabled=True,
)
sys.modules.setdefault("config", _stub)

from rag.redis_answer_cache import RedisAnswerCache  # noqa: E402


def main():
    cache = RedisAnswerCache(
        "redis://localhost:6379/0",
        ttl=60,
        prefix="test:acache:",
    )
    # 清掉上次的测试数据
    for k in cache._redis.scan_iter(match="test:acache:*", count=200):
        cache._redis.delete(k)

    srcs_ab = [{"document_id": "docA"}, {"document_id": "docB"}]
    srcs_b = [{"document_id": "docB"}]

    # 1. 稳定性
    t_ab_1 = cache.deps_token(srcs_ab)
    t_ab_2 = cache.deps_token(srcs_ab)
    assert t_ab_1 == t_ab_2, "同一批 sources 的 token 必须稳定"
    print(f"1. 同一批稳定      OK  {t_ab_1[:8]}")

    # 2. 不同依赖 → 不同 token
    t_b = cache.deps_token(srcs_b)
    assert t_ab_1 != t_b, "依赖不同文档的 token 必须不同"
    print(f"2. 区分不同依赖    OK  {t_b[:8]}")

    # 3 & 4. 只 bump docA
    cache.bump_document_version("docA")
    t_ab_after = cache.deps_token(srcs_ab)
    t_b_after = cache.deps_token(srcs_b)

    assert t_ab_after != t_ab_1, "bump docA 后，引用 docA 的 token 必须变"
    print(f"3. 引用者失效      OK  {t_ab_1[:8]} -> {t_ab_after[:8]}")

    assert t_b_after == t_b, "bump docA 后，不引用 docA 的 token 不能变"
    print(f"4. 无关者保留      OK  {t_b_after[:8]} 未变   <- 核心收益")

    # 5. 兜底
    before = cache.deps_token([{"text": "没有 id 的片段"}])
    cache.bump_collection_version()
    after = cache.deps_token([{"text": "没有 id 的片段"}])
    assert before != after, "识别不出 doc_id 时应退回全局版本"
    print("5. 无 doc_id 兜底  OK  退回全局版本")

    print("\n全部通过 -- 逻辑正确")


if __name__ == "__main__":
    main()
