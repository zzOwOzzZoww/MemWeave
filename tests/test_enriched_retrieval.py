import tempfile
import unittest
from pathlib import Path

from agent_knowledge_bridge.store import KnowledgeStore, retrieval_tokens


class EnrichedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temp.name) / "knowledge.db")

    def tearDown(self):
        self.temp.cleanup()

    def publish_active(self, *, search_terms=None):
        created = self.store.publish(
            source_agent="claude-code", project_key="p", title="Signing key rotation",
            content="Rotate signing keys before deployment.", knowledge_type="procedure",
            scope="project", evidence_summary="fixture", search_terms=search_terms,
        )
        knowledge_id = created["knowledge"]["id"]
        self.store.feedback(
            agent_id="reviewer", knowledge_id=knowledge_id, outcome="verified",
            evidence_kind="test", evidence_ref="fixture:test", evidence_summary="fixture",
        )
        return knowledge_id

    def test_numeric_fragments_do_not_create_fts_matches(self):
        self.assertEqual(retrieval_tokens("10 2026 -- .."), [])
        self.assertEqual(retrieval_tokens("calculate 10 calculate"), ["calculate"])
        self.publish_active()
        result = self.store.search(
            requester_agent="codex", project_key="p", query="calculate 10", limit=3
        )
        self.assertEqual(result["results"], [])

    def test_bilingual_aliases_are_resolved_by_lightweight_fts(self):
        knowledge_id = self.publish_active(search_terms="部署前 轮换 签名 密钥")
        result = self.store.search(
            requester_agent="codex", project_key="p",
            query="请记得在发布以前更换签名密钥", limit=3,
        )
        self.assertEqual([item["id"] for item in result["results"]], [knowledge_id])
        self.assertEqual(result["results"][0]["retrieval_method"], "fts5-enriched")

    def test_duplicate_publish_can_add_search_terms_without_new_record(self):
        knowledge_id = self.publish_active()
        duplicate = self.store.publish(
            source_agent="codex", project_key="p", title="Signing key rotation",
            content="Rotate signing keys before deployment.", knowledge_type="procedure",
            scope="project", evidence_summary="add aliases",
            search_terms="部署 轮换 签名 密钥",
        )
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["knowledge"]["id"], knowledge_id)
        self.assertEqual(duplicate["knowledge"]["search_terms"], "部署 轮换 签名 密钥")
        result = self.store.search(
            requester_agent="codex", project_key="p", query="部署时轮换签名密钥", limit=3
        )
        self.assertEqual(result["results"][0]["id"], knowledge_id)


if __name__ == "__main__":
    unittest.main()
