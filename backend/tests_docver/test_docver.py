"""Tests for per-document cache invalidation.

Covers the second iteration of the answer cache: the cache key depends on the
version of the documents an answer actually used, not on a single global
collection version.

Why this file lives in tests_docver/ instead of tests/rag/:
    tests/conftest.py imports the whole application (SQLAlchemy async, alembic,
    llm.factory -> openai, limiter -> slowapi, config -> cryptography). Those
    are runtime dependencies of the service, not of the cache logic. Stubbing
    `config` here keeps the test runnable with only `redis` + `fakeredis`
    installed, which also makes it cheap in CI.

    The stub is safe because `rag.answer_cache` only touches settings at call
    time (grounding policy, cache factory), never at import time.

Behaviour under test:
  * base AnswerCache keeps the old global-version behaviour
  * RedisAnswerCache derives a token from (document_id, version) pairs
  * bumping one document invalidates only the entries depending on it
  * falling back to the global version when no document_id is recognisable

Run with:
    python -m pytest tests_docver/ -q -o filterwarnings=
"""
import sys
import types

# ── Stub `config` before importing anything that needs it ────────────
# Must happen before `from rag...` below.
if "config" not in sys.modules:
    _stub = types.ModuleType("config")
    _stub.settings = types.SimpleNamespace(
        grounding_min_coverage=0.5,
        grounding_enforcement="strict",
        grounding_auto_cite_min_score=0.5,
        grounding_auto_cite_min_margin=0.1,
        redis_url="redis://localhost:6379/15",
        rag_answer_cache_ttl_seconds=60,
        rag_answer_cache_max_entries=100,
        rag_answer_cache_enabled=True,
        rag_answer_cache_backend="redis",
    )
    sys.modules["config"] = _stub

import pytest  # noqa: E402

from rag.answer_cache import AnswerCache  # noqa: E402
from rag.redis_answer_cache import RedisAnswerCache  # noqa: E402

fakeredis = pytest.importorskip("fakeredis")

DOC_A = "doc-a"
DOC_B = "doc-b"

SOURCES_A = [{"document_id": DOC_A, "chunk_id": "chunk-a1"}]
SOURCES_B = [{"document_id": DOC_B, "chunk_id": "chunk-b1"}]
SOURCES_AB = [
    {"document_id": DOC_A, "chunk_id": "chunk-a1"},
    {"document_id": DOC_B, "chunk_id": "chunk-b1"},
]


@pytest.fixture
def redis_cache():
    """RedisAnswerCache backed by an in-memory fake Redis."""
    cache = RedisAnswerCache(
        url="redis://localhost:6379/15",
        ttl=60,
        prefix="test:acache:",
    )
    # Construction does not connect; replace the connection object.
    cache._redis = fakeredis.FakeRedis(decode_responses=True)
    yield cache
    cache._redis.flushall()


# ── Base class keeps the old behaviour ────────────────────────────────


class TestBaseDepsToken:
    def test_token_follows_global_version(self):
        """Base AnswerCache has no per-document tracking."""
        cache = AnswerCache(ttl=60, max_entries=10)
        assert cache.deps_token(SOURCES_A) == "0"
        cache.bump_collection_version()
        assert cache.deps_token(SOURCES_A) == "1"

    def test_token_ignores_sources(self):
        """Without per-document versions the sources do not matter."""
        cache = AnswerCache(ttl=60, max_entries=10)
        assert cache.deps_token(SOURCES_A) == cache.deps_token(SOURCES_B)


# ── Redis backend: token derivation ───────────────────────────────────


class TestRedisDepsToken:
    def test_same_sources_produce_same_token(self, redis_cache):
        assert redis_cache.deps_token(SOURCES_A) == redis_cache.deps_token(SOURCES_A)

    def test_different_documents_produce_different_tokens(self, redis_cache):
        assert redis_cache.deps_token(SOURCES_A) != redis_cache.deps_token(SOURCES_B)

    def test_document_order_does_not_matter(self, redis_cache):
        """Token is built from a sorted set of pairs, so order is irrelevant."""
        reordered = list(reversed(SOURCES_AB))
        assert redis_cache.deps_token(SOURCES_AB) == redis_cache.deps_token(reordered)

    def test_new_document_starts_at_version_zero(self, redis_cache):
        assert redis_cache.document_version(DOC_A) == 0

    def test_bump_increments_document_version(self, redis_cache):
        assert redis_cache.bump_document_version(DOC_A) == 1
        assert redis_cache.bump_document_version(DOC_A) == 2
        assert redis_cache.document_version(DOC_A) == 2


# ── Selective invalidation: the point of the change ───────────────────


class TestSelectiveInvalidation:
    def test_bumping_a_changes_token_of_entries_using_a(self, redis_cache):
        before = redis_cache.deps_token(SOURCES_A)
        redis_cache.bump_document_version(DOC_A)
        assert redis_cache.deps_token(SOURCES_A) != before

    def test_bumping_a_keeps_token_of_entries_that_do_not_use_a(self, redis_cache):
        """The whole point: bumping A must not touch entries that only used B."""
        before_b = redis_cache.deps_token(SOURCES_B)
        before_ab = redis_cache.deps_token(SOURCES_AB)

        redis_cache.bump_document_version(DOC_A)

        assert redis_cache.deps_token(SOURCES_B) == before_b
        assert redis_cache.deps_token(SOURCES_AB) != before_ab

    def test_global_bump_does_not_affect_document_token(self, redis_cache):
        """A global bump must not invalidate entries whose documents did not change.

        This is what makes the global version a fallback rather than the
        primary mechanism.
        """
        before = redis_cache.deps_token(SOURCES_A)
        redis_cache.bump_collection_version()
        assert redis_cache.deps_token(SOURCES_A) == before


# ── Fallback when no document id is recognisable ──────────────────────


class TestFallback:
    def test_sources_without_document_id_fall_back_to_global(self, redis_cache):
        """Entries with no identifiable source cannot be invalidated selectively."""
        anonymous = [{"text": "a chunk without any document identifier"}]
        before = redis_cache.deps_token(anonymous)
        redis_cache.bump_collection_version()
        assert redis_cache.deps_token(anonymous) != before

    def test_empty_sources_fall_back_to_global(self, redis_cache):
        before = redis_cache.deps_token([])
        redis_cache.bump_collection_version()
        assert redis_cache.deps_token([]) != before
