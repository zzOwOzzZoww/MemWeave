"""Indexed exact-candidate lookup preserves collision and lifecycle guards."""
import pytest

from agent_knowledge_bridge.store import KnowledgeStore


def publish(store, title, content):
    return store.publish(source_agent='codex', project_key='p', title=title, content=content,
        knowledge_type='fact', scope='project', evidence_summary='Fixture', deduplicate_candidate_content=True)


@pytest.mark.parametrize('prefix', ['x' * 150, '中文前缀' * 40, 'text\x00' + 'x' * 145])
def test_same_prefix_and_length_are_never_enough_to_merge(tmp_path, prefix):
    store = KnowledgeStore(tmp_path / 'indexed.db')
    first = publish(store, 'First', prefix + 'A')['knowledge']['id']
    second = publish(store, 'Second', prefix + 'B')['knowledge']['id']
    assert first != second
    assert publish(store, 'Alternate', prefix + 'A')['knowledge']['id'] == first


def test_reopen_builds_index_without_rewriting_records_or_status(tmp_path):
    path = tmp_path / 'migration.db'
    store = KnowledgeStore(path)
    key = publish(store, 'Original', 'My service uses SQLite.')['knowledge']['id']
    with store._connect() as db:
        db.execute('DROP INDEX idx_candidate_body_lookup')
        before = dict(db.execute('SELECT * FROM knowledge_records WHERE id=?', (key,)).fetchone())
    reopened = KnowledgeStore(path)
    with reopened._connect() as db:
        assert dict(db.execute('SELECT * FROM knowledge_records WHERE id=?', (key,)).fetchone()) == before
        assert db.execute("SELECT name FROM sqlite_master WHERE name='idx_candidate_body_lookup'").fetchone()
    assert publish(reopened, 'New title', 'My service uses SQLite.')['knowledge']['id'] == key


def test_candidate_lookup_plan_uses_body_prefix_and_length_index(tmp_path):
    store = KnowledgeStore(tmp_path / 'plan.db')
    content = 'My service uses SQLite.'
    with store._connect() as db:
        plan = db.execute('''EXPLAIN QUERY PLAN SELECT * FROM knowledge_records
            WHERE project_key=? AND scope=? AND knowledge_type=? AND status='candidate'
            AND superseded_by IS NULL AND content=? AND length(content)=length(?)
            AND substr(content,1,128)=substr(?,1,128) AND COALESCE(evidence_speaker,'')=?
            AND (candidate_expires_at IS NULL OR candidate_expires_at>?)
            ORDER BY created_at,id LIMIT 1''',
            ('p', 'project', 'fact', content, content, content, '', '2026-10-16')).fetchall()
    details = ' '.join(row['detail'] for row in plan)
    assert 'SEARCH' in details and 'idx_candidate_body_lookup' in details
