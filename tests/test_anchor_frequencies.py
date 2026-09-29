import sqlite3
import unittest

from agent_knowledge_bridge.store import anchor_terms, bounded_document_frequencies


class AnchorFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(title, content, tokenize='unicode61')")
        self.db.executemany('INSERT INTO knowledge_fts VALUES (?, ?)', [
            ('第三方 Claude cache-nova signing_key', '第三方 CLAUDE café version.2'),
            ('第三方', 'cache nova café'),
            ('论文', 'common'), *[('common', 'common')] * 12,
        ])

    def tearDown(self):
        self.db.close()

    def exact(self, terms):
        return {term: self.db.execute(
            'SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?',
            ('"' + term.replace('"', '""') + '"',),
        ).fetchone()[0] for term in terms}

    def test_phrase_counts_match_original_threshold_decisions(self):
        terms = ['第三方', '论文', 'Claude', 'CLAUDE', 'cache-nova', 'signing_key',
                 'version.2', 'café', 'missing', 'common', 'quoted"word', '第三方']
        exact = self.exact(terms)
        counts = bounded_document_frequencies(self.db, terms, ceiling=6)
        self.assertEqual(counts, {term: min(count, 7) for term, count in exact.items()})
        expected = sorted((count, -len(term), term) for term, count in exact.items() if 0 < count <= 6)
        self.assertEqual(anchor_terms(self.db, terms), [row[2] for row in expected[:3]])

    def test_generated_concept_markers_never_become_rare_anchors(self):
        self.db.execute("INSERT INTO knowledge_fts VALUES ('marker', 'mwconcept_database cache-nova')")
        self.assertEqual(anchor_terms(self.db, ['mwconcept_database', 'cache-nova']), ['cache-nova'])

    def test_index_edits_and_rebuild_are_visible_without_cache_invalidation(self):
        def read():
            return bounded_document_frequencies(self.db, ['新知识', '第三方'], ceiling=6)
        self.assertEqual(read(), {'新知识': 0, '第三方': 2})
        self.db.execute("INSERT INTO knowledge_fts VALUES ('新知识', '第三方')")
        self.assertEqual(read(), {'新知识': 1, '第三方': 3})
        self.db.execute("DELETE FROM knowledge_fts WHERE title='新知识'")
        self.db.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
        self.assertEqual(read(), {'新知识': 0, '第三方': 2})

    def test_terms_use_one_bounded_batch_without_full_counts(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        bounded_document_frequencies(self.db, ['第三方', '论文', 'Claude', 'common'], ceiling=6)
        self.assertEqual(sum(sql.startswith('WITH terms') for sql in statements), 1)
        self.assertTrue(any('LIMIT 7' in sql for sql in statements))
        self.assertFalse(any('SELECT count(*) FROM knowledge_fts' in sql for sql in statements))

    def test_batch_failure_falls_back_to_bounded_match(self):
        db = self.db
        class WithoutBatch:
            def execute(self, sql, params=()):
                if sql.startswith('WITH terms'):
                    raise sqlite3.OperationalError('batch unavailable')
                return db.execute(sql, params)
        terms = ['第三方', 'common', 'cache-nova', 'missing']
        self.assertEqual(bounded_document_frequencies(WithoutBatch(), terms, ceiling=6),
                         {term: min(count, 7) for term, count in self.exact(terms).items()})


if __name__ == '__main__':
    unittest.main()
