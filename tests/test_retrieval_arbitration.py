"""Regression tests for retrieval arbitration.

These pin down the two ways retrieval can silently hand an agent the wrong
record: BM25 length normalisation letting a short generic record outrank the one
naming the entity the caller asked about, and a well-evidenced record losing to a
same-entity record that merely repeats the query wording more closely.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.store import retrieval_tokens


FILLER = (
    "Operations note. The service is deployed behind the shared ingress tier and "
    "restarts are coordinated with the platform team during the maintenance window. "
) * 12


class RetrievalArbitrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "knowledge.db"
        self.service = KnowledgeBridgeService(
            agent_id="claude-code",
            project_key="project-a",
            database_path=self.database_path,
        )
        self.store = self.service.store

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _publish(self, title: str, content: str, verify: int) -> str:
        created = self.service.publish(
            title=title,
            content=content,
            knowledge_type="procedure",
            evidence_summary="verifier passed",
        )
        knowledge_id = created["knowledge"]["id"]
        for index in range(verify):
            self.store.feedback(
                agent_id="claude-code",
                knowledge_id=knowledge_id,
                outcome="verified",
                evidence_summary="deterministic verifier passed",
                evidence_kind="test",
                evidence_ref=f"tests/{title}-{index}.json",
            )
        return knowledge_id

    def _raw_bm25_order(self, query: str) -> list[str]:
        tokens = retrieval_tokens(query)
        expression = " OR ".join(f'"{token}"' for token in tokens)
        with self.store._connect() as db:
            rows = db.execute(
                """
                SELECT knowledge_fts.knowledge_id AS id
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ?
                ORDER BY bm25(knowledge_fts) ASC
                """,
                (expression,),
            ).fetchall()
        return [row["id"] for row in rows]

    def test_shared_identifier_beats_bm25_length_normalisation(self) -> None:
        # A short record that repeats the query wording is what plain BM25 rewards.
        short = self._publish(
            "ledger-svc retry policy",
            "ledger-svc retry policy retry policy",
            verify=1,
        )
        # The long, well-evidenced record is the one that actually answers.
        long = self._publish(
            "ledger-svc retry policy timeout config",
            FILLER + "ledger-svc retry policy timeout config applies here.",
            verify=3,
        )
        query = "ledger-svc retry policy timeout config"

        self.assertEqual(self._raw_bm25_order(query)[0], short)

        result = self.service.search(query)

        self.assertEqual(result["results"][0]["id"], long)
        self.assertEqual(result["results"][0]["verified_count"], 3)

    def test_evidence_breaks_ties_within_the_same_entity(self) -> None:
        weak = self._publish(
            "ledger-svc retry policy",
            "ledger-svc retry policy retry policy",
            verify=1,
        )
        strong = self._publish(
            "ledger-svc retry policy timeout config",
            FILLER + "ledger-svc retry policy timeout config applies here.",
            verify=3,
        )

        result = self.service.search("ledger-svc retry policy timeout config")

        ids = [item["id"] for item in result["results"]]
        self.assertEqual(ids, [strong, weak])

    def test_equal_evidence_preserves_incoming_bm25_order(self) -> None:
        long = self._publish(
            "ledger-svc verbose procedure",
            FILLER + "ledger-svc retry policy timeout config applies here.",
            verify=1,
        )
        short = self._publish(
            "ledger-svc retry policy",
            "ledger-svc retry policy timeout config",
            verify=1,
        )
        query = "ledger-svc retry policy timeout config"

        self.assertEqual(self._raw_bm25_order(query)[:2], [short, long])
        result = self.service.search(query)

        self.assertEqual([item["id"] for item in result["results"]], [short, long])

    def test_unrelated_record_is_not_pinned_above_the_named_entity(self) -> None:
        other = self._publish("cache-svc retry policy", "cache-svc retry policy", verify=5)
        named = self._publish(
            "ledger-svc retry policy", "ledger-svc retry policy", verify=1
        )

        result = self.service.search("ledger-svc retry policy")

        self.assertEqual(result["results"][0]["id"], named)
        self.assertNotEqual(result["results"][0]["id"], other)

    def test_exact_stale_match_survives_a_large_active_candidate_pool(self) -> None:
        target = self._publish(
            "target-record procedure",
            "target-record alpha exact answer",
            verify=1,
        )
        self.store.transit(
            target,
            to_status="stale",
            reason="test fixture",
            actor="test-agent",
        )
        for index in range(30):
            self._publish(
                f"noise-{index}",
                f"alpha generic noise {index}",
                verify=1,
            )

        result = self.service.search("target-record alpha", limit=3)

        self.assertEqual(result["results"][0]["id"], target)


class SiblingExpansionTest(unittest.TestCase):
    """A hit must arrive with the work it belongs to, not on its own.

    Retrieval matches one record at a time. A corpus that accumulates dozens of
    records about one subject therefore answers a query with whichever record
    happens to share its wording, and the caller holds a fact with no idea which
    paper or project it constrains -- it learns the phrase "third-party relay"
    and not which paper that phrase is about. Expansion bridges a record to the
    cluster around it; these cases pin its three obligations: it must bridge on
    whole words rather than CJK window fragments, it must never disturb the order
    the query itself produced, and it must label and attribute what it added.
    """

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "knowledge.db"
        self.service = KnowledgeBridgeService(
            agent_id="claude-code",
            project_key="project-a",
            database_path=self.database_path,
        )
        self.store = self.service.store

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _publish(self, title: str, content: str) -> str:
        created = self.service.publish(
            title=title,
            content=content,
            knowledge_type="procedure",
            evidence_summary="verifier passed",
        )
        knowledge_id = created["knowledge"]["id"]
        # A fresh record is a `candidate` and retrieval deliberately cannot see
        # it; only verified, objectively evidenced feedback promotes it to
        # `active`. Without this the fixture would test an empty corpus.
        self.store.feedback(
            agent_id="claude-code",
            knowledge_id=knowledge_id,
            outcome="verified",
            evidence_summary="deterministic verifier passed",
            evidence_kind="test",
            evidence_ref=f"tests/{title}.json",
        )
        return knowledge_id

    def test_bridge_requires_shared_words_not_window_fragments(self) -> None:
        """Fragments exist to MATCH with; they must never decide relatedness.

        ``retrieval_tokens`` slices a Han run into every 2- and 3-gram it holds,
        so two unrelated Chinese sentences drawn from the same character pool
        share tokens by construction. Scoring siblings on those would declare any
        two long Chinese records to be about the same subject.
        """
        from agent_knowledge_bridge.store import _whole_terms

        anchor = self._publish(
            "论文第三方渠道",
            "论文正文不披露第三方渠道，只写平台名称。",
        )
        decoy = self._publish(
            "测试环境说明",
            "第三方公司提供的测试环境不对外开放，正文只写部署方式。",
        )

        anchor_terms = _whole_terms(
            "论文第三方渠道 "
            "论文正文不披露第三方渠道，只写平台名称。"
        )
        decoy_terms = _whole_terms(
            "测试环境说明 "
            "第三方公司提供的测试环境不对外开放，正文只写部署方式。"
        )
        self.assertLess(
            len(anchor_terms & decoy_terms),
            len(anchor_terms) // 2,
            "fixture is wrong: the decoy must not be a real topical neighbour",
        )

        result = self.service.search("论文第三方渠道", limit=4)

        self.assertEqual(result["results"][0]["id"], anchor)
        self.assertNotIn(decoy, [item["id"] for item in result["results"]])

    def test_siblings_never_displace_the_direct_hits(self) -> None:
        """The query's own answer keeps its rank; the cluster lands behind it."""
        anchor = self._publish(
            "ledger-svc retry policy",
            "ledger-svc retry policy uses exponential backoff.",
        )
        runner_up = self._publish(
            "ledger-svc retry policy timeout",
            "ledger-svc retry policy timeout is thirty seconds.",
        )
        self.assertNotEqual(anchor, runner_up)

        without = self.service.search(
            "ledger-svc retry policy", limit=3, expand_siblings=False
        )
        with_expansion = self.service.search("ledger-svc retry policy", limit=3)

        self.assertEqual(
            [item["id"] for item in without["results"]][:2], [anchor, runner_up]
        )
        self.assertEqual(
            [item["id"] for item in with_expansion["results"]][:2],
            [item["id"] for item in without["results"]][:2],
        )

    def test_added_records_are_labelled_and_point_at_their_anchor(self) -> None:
        """An inferred neighbour must be distinguishable from a matched record.

        Recall accounting and any reader of the context block need to know the
        query reached the anchor and did not reach this. The label alone is not
        enough: ``related_to`` names the record the inference came from, which is
        what turns "it knows the phrase" into "it knows which paper".

        The neighbour shares the *subject* and no query term at all -- its title
        carries the topic words while the rare identifier appears only in the
        other records' bodies. That is the shape the real failure took: the
        agent knew the topic and had no way to reach the record about it.
        """
        anchor = self._publish(
            "backoff procedure",
            "clb-v2 backoff applies to this service.",
        )
        self._publish(
            "backoff procedure two",
            "clb-v2 backoff also applies here alongside extended service notes "
            "about retries, latency, budgets, safeguards, and deployment procedures.",
        )
        neighbour = self._publish(
            "backoff procedure notes",
            "unrelated body text carrying no identifier.",
        )

        direct = self.service.search("clb-v2", limit=2, expand_siblings=False)
        self.assertNotIn(
            neighbour,
            [item["id"] for item in direct["results"]],
            "fixture is wrong: the neighbour must be below the direct cut",
        )
        self.assertEqual(direct["results"][0]["id"], anchor)

        result = self.service.search("clb-v2", limit=2)
        by_id = {item["id"]: item for item in result["results"]}

        self.assertEqual(by_id[anchor]["origin"], "direct")
        self.assertNotIn("related_to", by_id[anchor])
        self.assertEqual(by_id[neighbour]["origin"], "sibling")
        self.assertEqual(by_id[neighbour]["related_to"], anchor)


class VocabularyBridgeTest(unittest.TestCase):
    """The bridge must be a graph built from the corpus, and it must be honest.

    Lexical retrieval fails across Agents for a reason no ranking change can
    reach: two Agents name one subject differently, so the records are about the
    same thing and share no term. Retrieval is not misordering them -- they never
    enter the pool at all. The bridge is derived from the records themselves, and
    these cases pin the three ways that derivation can go quietly wrong: it can
    drop half the vocabulary on an encoding accident, its counts can drift each
    time it is rebuilt, and it can manufacture a query out of nothing when no
    association actually cleared the floor.
    """

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "knowledge.db"
        self.service = KnowledgeBridgeService(
            agent_id="claude-code",
            project_key="project-a",
            database_path=self.database_path,
        )
        self.store = self.service.store

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _publish(self, title: str, content: str, search_terms: str) -> str:
        created = self.service.publish(
            title=title,
            content=content,
            knowledge_type="procedure",
            evidence_summary="verifier passed",
            search_terms=search_terms,
        )
        knowledge_id = created["knowledge"]["id"]
        # A candidate is deliberately invisible to retrieval; only verified,
        # objectively evidenced feedback promotes it. Without this the bridge
        # would be derived from records the corpus cannot serve.
        self.store.feedback(
            agent_id="claude-code",
            knowledge_id=knowledge_id,
            outcome="verified",
            evidence_summary="deterministic verifier passed",
            evidence_kind="test",
            evidence_ref=f"tests/{knowledge_id}.json",
        )
        return knowledge_id

    def _edges(self, term: str) -> list[tuple[str, int]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT CASE WHEN term_a = ? THEN term_b ELSE term_a END AS other,
                       seen_count
                FROM term_cooccurrence
                WHERE project_key = 'project-a' AND (term_a = ? OR term_b = ?)
                ORDER BY seen_count DESC
                """,
                (term, term, term),
            ).fetchall()
        return [(row["other"], row["seen_count"]) for row in rows]

    def test_term_selection_is_not_decided_by_encoding_order(self) -> None:
        """A CJK term must survive the per-record cap.

        The cap used to be ``sorted(terms)[:24]``. Sorting is by codepoint and
        every Latin letter sorts below every Han character, so the cap kept the
        ASCII half of each record and discarded its Chinese terms -- on the real
        corpus 58% of the vocabulary never reached the graph. A record carrying
        more terms than the cap allows must still contribute its Chinese ones.
        """
        latin = " ".join(f"token{index}" for index in range(20))
        self._publish(
            title="中转 endpoint relay 披露口径",
            content="第三方访问路径的披露边界。",
            search_terms=f"{latin} 第三方中转 端点披露",
        )
        # The same association seen a second time, which is what lifts it to the
        # floor the query side serves from.
        self._publish(
            title="中转 endpoint 披露补充",
            content="访问路径的补充说明。",
            search_terms="中转 endpoint 披露",
        )
        self.assertTrue(
            self._edges("中转"),
            "a Chinese term was dropped by the cap: the ASCII terms sorted "
            "ahead of every CJK term and consumed the whole budget",
        )

    def test_rebuild_is_idempotent(self) -> None:
        """Counts describe the corpus, not how often the graph was rebuilt.

        ``seen_count`` is what separates a real variant pairing from one
        author's phrasing, so it is served as a threshold. When it was
        incremented on every write, replaying the backfill doubled every count
        and promoted noise past the floor. The graph must be the same table
        however many times it is derived.
        """
        self._publish(
            title="中转 endpoint 披露",
            content="访问路径的说明。",
            search_terms="中转 relay endpoint 披露",
        )
        self._publish(
            title="复现包装配步骤",
            content="打包与校验流程。",
            search_terms="复现包 artifact 校验",
        )
        first = self.store.rebuild_term_bridges()
        second = self.store.rebuild_term_bridges()
        self.assertEqual(first, second)

    def test_thin_graph_does_not_invent_a_query(self) -> None:
        """No qualifying association must mean no second retrieval pass.

        The extension lookup aggregates with ``MAX``. Without a ``GROUP BY`` an
        aggregate over zero matching rows still returns one row, and ``MAX`` over
        an empty set is NULL -- so a term whose every edge sat below the floor
        produced a "candidate" of NULL, which was appended to the match
        expression as a bogus token and answered with records the corpus never
        connected to the query. This pins that an empty graph leaves the direct
        result set untouched.
        """
        only = self._publish(
            title="代理配置说明",
            content="配置项的含义与默认取值。",
            search_terms="代理 配置",
        )
        result = self.service.search("代理 配置", limit=5)
        self.assertEqual(
            [item["origin"] for item in result["results"]],
            ["direct"],
            "a record was served on an expansion the graph never justified",
        )
        self.assertEqual(result["results"][0]["id"], only)

    def test_a_real_vocabulary_gap_is_bridged_and_labelled(self) -> None:
        """The case the feature exists for: a query naming its own variant.

        Cross-record edges require the two records to already share a term --
        that guard is what keeps unrelated records from being linked by
        coincidence. So the fixture is the shape the real corpus takes: a
        bridging record that shares wording with both sides, teaching the corpus
        that `代理` and `中转` name one subject. The target then shares *nothing*
        with the query, which is the whole point.

        The caller must be told which happened, because a result not matched by
        the words actually asked with should not look like one that was.

        Two records assert the bridging association, not one. ``seen_count`` is
        the number of records that assert a pairing and the query side serves
        only pairings at or above ``MIN_COOCCURRENCE`` -- one author saying a
        thing once is phrasing, and the floor is what keeps a single unusual
        record from permanently redefining a common term. A lone bridging record
        correctly produces edges at one, and correctly arrives as a direct hit
        with no bridge behind it.
        """
        self._publish(
            title="代理 中转 配置说明",
            content="两者在同一处配置。",
            search_terms="代理 中转 配置",
        )
        # The same association, asserted a second time. This is what the real
        # corpus looks like: an Agent that resolves a variant records the
        # resolution more than once, and that repetition is the evidence the
        # query side thresholds on.
        self._publish(
            title="代理 中转 路由备注",
            content="同一处配置的补充说明。",
            search_terms="代理 中转 路由",
        )
        target = self._publish(
            title="endpoint 披露口径",
            content="访问路径的说明。",
            search_terms="endpoint 披露 中转",
        )
        result = self.service.search("代理 配置", limit=5)
        by_id = {item["id"]: item for item in result["results"]}
        self.assertIn(target, by_id, f"the gap was not bridged: {sorted(by_id)}")
        self.assertEqual(by_id[target]["origin"], "bridged")
        # A bridged record was reached by restating the query, not by way of
        # another record, so it must not claim an anchor it does not have.
        self.assertNotIn("related_to", by_id[target])


if __name__ == "__main__":
    unittest.main()
