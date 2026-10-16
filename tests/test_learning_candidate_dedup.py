"""Only exact, current candidate bodies may reuse a model's alternate title."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.learning_engine import LearningEngine
from agent_knowledge_bridge.store import KnowledgeStore
from test_tiered_admission import proposal


def publish(store, title, **extra):
    return store.publish(source_agent='codex', project_key='p', title=title,
        content=extra.pop('content', 'My service uses SQLite.'), knowledge_type=extra.pop('knowledge_type', 'fact'),
        scope=extra.pop('scope', 'project'), evidence_summary='Fixture evidence',
        deduplicate_candidate_content=True, **extra)


def test_alternate_titles_share_a_candidate_and_preserve_both_sources(tmp_path):
    path = tmp_path / 'learning.db'
    text = 'My service uses SQLite.'
    for agent, session, title in [('codex', 'a', 'Storage choice'), ('gemini-cli', 'b', 'Service database')]:
        engine = LearningEngine(database_path=path, agent_id=agent, project_key='p',
            transcript_parser=lambda *a, **k: TranscriptTurn(text, '', ()),
            reviewer=lambda _, title=title: {'proposals': [proposal(text, kind='fact', title=title)]})
        assert engine.learn({'session_id': session})['promoted'] == 0
    with engine.store.knowledge._connect() as db:
        rows = db.execute('SELECT * FROM knowledge_records').fetchall()
        assert len(rows) == 1 and rows[0]['status'] == 'candidate' and rows[0]['title'] == 'Storage choice'
        assert {r[0] for r in db.execute('SELECT agent_id FROM knowledge_evidence')} == {'codex', 'gemini-cli'}
        assert {r[0] for r in db.execute('''SELECT r.session_id FROM learning_runs r
            JOIN knowledge_compilation_links l ON l.run_id=r.id''')} == {'a', 'b'}


@pytest.mark.parametrize('change', [
    {'content': 'My service does not use SQLite.'}, {'knowledge_type': 'decision'},
    {'scope': 'user'}, {'evidence_speaker': 'another-person'},
])
def test_different_assertions_types_scopes_and_speakers_do_not_merge(tmp_path, change):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    first = publish(store, 'First')['knowledge']['id']
    second = publish(store, 'Second', **change)
    assert not second['deduplicated'] and second['knowledge']['id'] != first


@pytest.mark.parametrize('state', ['active', 'stale', 'quarantined', 'archived'])
def test_exact_body_dedup_cannot_borrow_status_or_revive_old_records(tmp_path, state):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    first = publish(store, 'First')['knowledge']['id']
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET status=? WHERE id=?', (state, first))
    second = publish(store, 'Second')
    assert not second['deduplicated'] and second['knowledge']['status'] == 'candidate'
    with store._connect() as db:
        assert db.execute('SELECT status FROM knowledge_records WHERE id=?', (first,)).fetchone()[0] == state


def test_other_projects_and_expired_candidates_are_not_reused(tmp_path):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    first = publish(store, 'First')['knowledge']['id']
    with store._connect() as db:
        db.execute("UPDATE knowledge_records SET project_key='other' WHERE id=?", (first,))
    second = publish(store, 'Second')['knowledge']['id']
    assert first != second
    with store._connect() as db:
        db.execute("UPDATE knowledge_records SET candidate_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (second,))
    assert publish(store, 'Third')['knowledge']['id'] not in {first, second}


def test_concurrent_candidate_reuse_is_transactional(tmp_path):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: publish(store, 'Title ' + str(i)), range(4)))
    assert len({item['knowledge']['id'] for item in results}) == 1
    assert sum(not item['deduplicated'] for item in results) == 1


def test_alternate_title_does_not_overwrite_first_index_metadata(tmp_path):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    first = publish(store, 'First', search_terms='SQLite', subject_terms=['SQLite'])['knowledge']['id']
    publish(store, 'Second', search_terms='unrelated crash', subject_terms=['unrelated'])
    with store._connect() as db:
        record = db.execute('SELECT * FROM knowledge_records WHERE id=?', (first,)).fetchone()
        assert record['title'] == 'First' and record['search_terms'] == 'SQLite'


def test_manual_publication_keeps_existing_title_sensitive_identity(tmp_path):
    store = KnowledgeStore(tmp_path / 'dedup.db')
    args = dict(source_agent='codex', project_key='p', content='My service uses SQLite.',
        knowledge_type='fact', scope='project', evidence_summary='Fixture')
    first = store.publish(title='First', **args)['knowledge']['id']
    assert store.publish(title='Second', **args)['knowledge']['id'] != first
