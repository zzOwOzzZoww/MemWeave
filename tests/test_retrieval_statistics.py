"""Cache invalidation, process boundaries, transaction snapshots and bounds."""
import os
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest

from agent_knowledge_bridge.retrieval_stats import FrequencyCache, StatisticsSnapshot
from agent_knowledge_bridge.store import (
    SEARCH_INDEX_VERSION, KnowledgeStore, bounded_document_frequencies,
    indexed_content, retrieval_tokens,
)
from agent_knowledge_bridge.retrieval_terms import concept_tokens
from agent_knowledge_bridge.service import KnowledgeBridgeService


@pytest.fixture
def service(tmp_path):
    return KnowledgeBridgeService(agent_id="claude-code", project_key="p", database_path=tmp_path / "db.sqlite")


def publish(service, title="alpha", content="datum"):
    return service.publish(title=title, content=content, knowledge_type="fact",
                            evidence_summary="fixture")["knowledge"]["id"]


def test_query_and_index_share_stable_concept_markers():
    query = "对话内容发给云端模型前必须做什么？"
    content = "Redact credentials before sending conversation excerpts to a remote model provider."
    query_markers = set(concept_tokens(query))
    indexed = set(indexed_content("Remote handling", content, "").split())
    assert query_markers
    assert query_markers <= set(retrieval_tokens(query))
    assert {"mwconcept_conversation", "mwconcept_model", "mwconcept_remote"} <= query_markers & indexed


def test_concept_index_migration_runs_once(service):
    publish(service, title="远程模型", content="发送对话前先处理凭据。")
    first = service.store.rebuild_search_index(only_if_outdated=True)
    second = service.store.rebuild_search_index(only_if_outdated=True)
    assert first == {"records": 1, "changed": True, "version": SEARCH_INDEX_VERSION}
    assert second == {"records": 0, "changed": False, "version": SEARCH_INDEX_VERSION}
    with service.store._connect() as db:
        indexed = db.execute("SELECT content FROM knowledge_fts").fetchone()[0]
        assert "mwconcept_conversation" in indexed
        assert db.execute(
            "SELECT count(*) FROM knowledge_migrations WHERE version=?",
            (SEARCH_INDEX_VERSION,),
        ).fetchone()[0] == 1


def snapshot(store, db, loader=bounded_document_frequencies, cache=None):
    return StatisticsSnapshot(db, store.database_path, loader, cache=cache)


def measure(store, terms=("alpha",), cache=None, loader=bounded_document_frequencies):
    with store._connect() as db:
        db.execute("BEGIN")
        stats = snapshot(store, db, loader, cache)
        values = stats.frequencies(terms, ceiling=6)
        return values, stats.metrics(), stats.namespace


def test_warm_cache_shared_by_store_objects_skips_match_count(service):
    publish(service)
    cache, loader = FrequencyCache(), Mock(wraps=bounded_document_frequencies)
    first = measure(service.store, cache=cache, loader=loader)
    second_store = KnowledgeStore(service.store.database_path)
    second = measure(second_store, cache=cache, loader=loader)
    assert first[0] == second[0] == {"alpha": 1}
    assert loader.call_count == 1
    assert second[1]["hits"] == 1 and second[1]["batches"] == 0


@pytest.mark.parametrize("field,value", [
    ("title", "beta"), ("content", "beta datum"), ("search_terms", "beta"),
    ("status", "archived"), ("scope", "user"), ("project_key", "another"),
])
def test_record_edits_invalidate_even_when_count_is_unchanged(service, field, value):
    key = publish(service)
    before = measure(service.store)
    with service.store._connect() as db:
        db.execute(f"UPDATE knowledge_records SET {field}=? WHERE id=?", (value, key))
        if field in {"title", "content", "search_terms"}:
            r = db.execute("SELECT * FROM knowledge_records WHERE id=?", (key,)).fetchone()
            db.execute("DELETE FROM knowledge_fts WHERE knowledge_id=?", (key,))
            db.execute("INSERT INTO knowledge_fts(knowledge_id,title,content) VALUES (?,?,?)",
                       (key, r["title"], indexed_content(r["title"], r["content"], r["search_terms"])))
    after = measure(service.store)
    assert before[2] != after[2]
    assert after[1]["misses"] == 1
    with service.store._connect() as db:
        assert after[0] == bounded_document_frequencies(db, ["alpha"], ceiling=6)


def test_publish_and_duplicate_alias_updates_invalidate(service):
    first = measure(service.store, ("beta",))
    publish(service)
    inserted = measure(service.store, ("beta",))
    assert first[2] != inserted[2]
    service.publish(title="alpha", content="datum", search_terms="beta", knowledge_type="fact",
                    evidence_summary="fixture alias update")
    updated = measure(service.store, ("beta",))
    assert updated[0] == {"beta": 1}
    assert updated[2] != inserted[2]


def test_delete_and_rebuild_invalidate(service):
    key = publish(service)
    before = measure(service.store)
    with service.store._connect() as db:
        db.execute("DELETE FROM knowledge_evidence WHERE knowledge_id=?", (key,))
        db.execute("DELETE FROM knowledge_fts WHERE knowledge_id=?", (key,))
        db.execute("DELETE FROM knowledge_records WHERE id=?", (key,))
    after = measure(service.store)
    assert after[0] == {"alpha": 0}
    assert after[2] != before[2]
    service.store.rebuild_search_index()
    rebuilt = measure(service.store)
    assert rebuilt[0] == after[0] and rebuilt[2] != after[2]


def test_hits_and_noop_updates_do_not_flush_statistics(service):
    key = publish(service)
    before = measure(service.store)
    service.store.mark_hits([key])
    with service.store._connect() as db:
        db.execute("UPDATE knowledge_records SET title=title,verified_count=verified_count+1 WHERE id=?", (key,))
    after = measure(service.store)
    assert after[2] == before[2]
    assert after[1]["hits"] == 1


def test_external_process_committed_write_is_visible(service):
    key = publish(service)
    before = measure(service.store)
    code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute(\"UPDATE knowledge_records SET status='archived' WHERE id=?\",(sys.argv[2],)); c.commit(); c.close()")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(service.store.database_path), key],
                            capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0
    after = measure(service.store)
    assert after[2] != before[2] and after[1]["misses"] == 1


def test_wal_reader_uses_one_version_for_statistics_and_candidates(service):
    key = publish(service)
    with service.store._connect() as reader:
        reader.execute("BEGIN")
        stats = snapshot(service.store, reader, cache=FrequencyCache())
        assert stats.frequencies(["alpha"], ceiling=6) == {"alpha": 1}
        publish(service, "alpha second", "another datum")
        assert stats.frequencies(["alpha"], ceiling=6) == {"alpha": 1}
        assert reader.execute("SELECT count(*) FROM knowledge_records").fetchone()[0] == 1
        fresh_stats_same_snapshot = snapshot(service.store, reader, cache=FrequencyCache())
        assert fresh_stats_same_snapshot.frequencies(["alpha"], ceiling=6) == {"alpha": 1}
    assert measure(service.store)[0] == {"alpha": 2}


def test_rollback_epoch_cannot_poison_future_revision(service):
    key = publish(service)
    baseline = measure(service.store)
    with service.store._connect() as db:
        db.execute("BEGIN")
        db.execute("UPDATE knowledge_records SET title='rolled-back' WHERE id=?", (key,))
        db.execute("DELETE FROM knowledge_fts WHERE knowledge_id=?", (key,))
        stats = snapshot(service.store, db)
        rolled_back_namespace = stats.namespace
        assert stats.frequencies(["alpha"], ceiling=6) == {"alpha": 0}
        rolled_back_revision = stats.revision
        db.rollback()
    assert measure(service.store)[0] == baseline[0]
    service.store.transit(key, to_status="archived", reason="fixture", actor="test")
    after = measure(service.store)
    assert after[1]["revision"] == rolled_back_revision
    assert after[2] != rolled_back_namespace
    assert after[0] == {"alpha": 1}


def test_scope_filter_remains_separate_from_global_df(service):
    publish(service)
    foreign = KnowledgeBridgeService(agent_id="codex", project_key="other",
                                    database_path=service.store.database_path)
    publish(foreign, "alpha foreign", "private")
    assert measure(service.store)[0] == {"alpha": 2}
    result = service.store.search(requester_agent="codex", project_key="p", query="alpha", limit=3)
    assert result["count"] == 0  # candidates have not been verified


def test_database_recreation_and_threshold_do_not_share_wrong_cache(service):
    publish(service)
    before = measure(service.store)
    with service.store._connect() as db:
        db.execute("BEGIN")
        stats = snapshot(service.store, db)
        assert stats.frequencies(["alpha"], ceiling=0) == {"alpha": 1}
        assert stats.metrics()["misses"] == 1
    replacement = service.store.database_path.with_name("replacement.db")
    KnowledgeStore(replacement)
    os.replace(replacement, service.store.database_path)
    after = measure(KnowledgeStore(service.store.database_path))
    assert after[2] != before[2] and after[0] == {"alpha": 0}


def test_cache_lru_entry_and_estimated_byte_budgets():
    cache = FrequencyCache(max_entries=2, max_bytes=1000)
    cache.put(("a",), 1)
    cache.put(("b",), 2)
    assert cache.get(("a",)) == 1
    cache.put(("c",), 3)
    assert cache.get(("b",)) is None
    cache.put(("a",), 0)
    assert cache.get(("a",)) == 0
    for i in range(30):
        cache.put((str(i) * 100,), i)
    info = cache.info()
    assert info["entries"] <= 2 and info["estimated_bytes"] <= 1000
    cache.put(("oversize" * 1000,), 1)
    assert cache.info() == info


def test_statistics_require_explicit_transaction(service):
    with service.store._connect() as db, pytest.raises(ValueError, match="snapshot"):
        snapshot(service.store, db)


def test_index_rebuild_preserves_enriched_search_and_invalidates_cache(service):
    key = service.publish(title='论文标题', content='只写平台名称', search_terms='relay-platform 论文规范',
                          knowledge_type='fact', evidence_summary='fixture')['knowledge']['id']
    service.store.feedback(agent_id='claude-code', knowledge_id=key, outcome='verified',
                           evidence_summary='verified', evidence_kind='test', evidence_ref='tests/fixture')
    args = dict(requester_agent='codex', project_key='p', query='relay-platform', limit=3)
    before = service.store.search(**args)
    service.store.rebuild_search_index()
    after = service.store.search(**args)
    assert [r['id'] for r in after['results']] == [r['id'] for r in before['results']] == [key]
    assert after['retrieval_diagnostics']['statistics']['revision'] > before['retrieval_diagnostics']['statistics']['revision']


def test_learning_trace_does_not_invalidate_df_cache(service):
    from agent_knowledge_bridge.reuse import ReuseStore
    publish(service)
    before = measure(service.store)
    reuse = ReuseStore(service.store.database_path)
    reuse.start(agent_id='codex', project_key='p', session_id='fixture', prompt='alpha',
                records=[], retrieval_ms=1)
    after = measure(service.store)
    assert after[2] == before[2] and after[1]['hits'] == 1
