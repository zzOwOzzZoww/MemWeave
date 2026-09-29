"""Composition contracts, plus default-policy equivalence to the frozen oracle."""
import itertools
import json
from types import SimpleNamespace

import pytest

from agent_knowledge_bridge.retrieval_pipeline import (
    Candidate, RetrievalPolicy, StageContext, expand, arbitrate, truncate)
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.reuse import ReuseStore
from fixtures.retrieval_before_stages import LegacyKnowledgeStore


def row(key):
    return {"id": key, "title": "Topic " + key, "rank": 0.0}


class Routes:
    def _expand_vocabulary(self, db, **kwargs):
        return [row("b1"), row("b2")]

    def _sibling_additions(self, db, rows, **kwargs):
        parent = next((r for r in rows if r["id"] == "b1"), rows[0])
        return [row("s")], {"s": parent["id"]}

    def _expand_anchored(self, db, **kwargs):
        return [row("a")][:kwargs["room"]]


def context(policy=None, limit=2):
    return StageContext(Routes(), None, "query", "p", "('active','stale')",
                        limit, False, policy or RetrievalPolicy(), None)


@pytest.mark.parametrize("flags", list(itertools.product((False, True), repeat=3)))
def test_eight_route_combinations(flags):
    names = tuple(n for n, on in zip(("bridge", "sibling", "anchor"), flags) if on)
    ctx = context(RetrievalPolicy(stages=names))
    pool, reports = expand(ctx, [])
    selected, _ = truncate(arbitrate(pool, [], 2), limit=2, slack=2)
    expected = (["b1", "s"] if flags[0] and flags[1] else
                ["b1", "b2"] if flags[0] else [])
    if flags[2]:
        expected.append("a")
    assert [c.id for c in selected] == expected
    assert [r["stage"] for r in reports] == ["bridge", "sibling", "anchor"]
    assert not any(c.depth > 2 for c in pool)


@pytest.mark.parametrize("policy,expected_sibling", [
    (RetrievalPolicy(), True),
    (RetrievalPolicy(sibling_seed_origins=("direct",)), False),
    (RetrievalPolicy(max_depth=1), False),
    (RetrievalPolicy(max_depth=0), False),
])
def test_bridge_sibling_dependency_and_depth_are_explicit(policy, expected_sibling):
    pool, _ = expand(context(policy), [])
    siblings = [c for c in pool if c.origin == "sibling"]
    assert bool(siblings) == expected_sibling
    if siblings:
        assert (siblings[0].parent_id, siblings[0].parent_origin, siblings[0].depth) == ("b1", "bridged", 2)


def test_one_hop_policy_still_allows_sibling_from_direct():
    pool, _ = expand(context(RetrievalPolicy(max_depth=1)), [row("d1"), row("d2")])
    assert next(c for c in pool if c.id == "s").depth == 1


def test_anchor_extends_page_and_does_not_displace_protected_hits():
    pool, _ = expand(context(RetrievalPolicy(stages=("anchor",))), [row("d1"), row("d2")])
    selected, _ = truncate(arbitrate(pool, [], 2), limit=2, slack=2)
    assert [c.id for c in selected] == ["d1", "d2", "a"]


def test_expansion_retains_pool_until_final_budget():
    pool, _ = expand(context(), [row("d1"), row("d2"), row("d3")])
    assert {"d1", "d2", "d3", "s", "a"} == {c.id for c in pool}
    selected, omitted = truncate(arbitrate(pool, [], 2), limit=2, slack=2)
    assert [c.id for c in selected] == ["d1", "s", "a"]
    assert {x["knowledge_id"] for x in omitted} == {"d2", "d3"}
    assert all(x["reason"] == "row_budget" for x in omitted)


def test_duplicate_routes_preserve_paths_and_legacy_attribution():
    pool = (Candidate(row("d")), Candidate(row("b"), "bridged", depth=1),
            Candidate(row("s"), "sibling", "b", "Topic b", "bridged", 2),
            Candidate(row("b"), "anchored", depth=1))
    ranked = arbitrate(pool, [], 2)
    selected, _ = truncate(ranked, limit=2, slack=2)
    assert [c.id for c in selected] == ["d", "s", "b"]
    assert selected[-1].origin == "bridged"
    assert {p["origin"] for p in ranked.provenance["b"]} == {"bridged", "anchored"}
    direct = Candidate(row("b"))
    selected, _ = truncate(arbitrate((direct, *pool), [], 2), limit=2, slack=2)
    assert len({c.id for c in selected}) == len(selected)


def test_fallback_only_when_all_routes_empty():
    fallback = [row("fallback")]
    assert truncate(arbitrate((), fallback, 2), limit=2, slack=2)[0][0].id == "fallback"
    selected, omitted = truncate(arbitrate((Candidate(row("d")),), fallback, 2), limit=2, slack=2)
    assert [c.id for c in selected] == ["d"]
    assert omitted[0]["reason"] == "fallback_not_needed"


def test_disabled_expansion_never_calls_any_route():
    ctx = context()
    object.__setattr__(ctx, "store", SimpleNamespace())
    pool, reports = expand(ctx, [row("d")], enabled=False)
    assert [c.id for c in pool] == ["d"]
    assert all(r["skipped_reason"] == "disabled" for r in reports)


def test_stage_enablement_does_not_change_execution_order():
    pool, reports = expand(context(RetrievalPolicy(stages=("anchor", "sibling", "bridge"))), [])
    assert [r["stage"] for r in reports] == ["bridge", "sibling", "anchor"]
    assert next(c for c in pool if c.id == "s").parent_origin == "bridged"


@pytest.fixture
def service(tmp_path):
    return KnowledgeBridgeService(agent_id="claude-code", project_key="p", database_path=tmp_path / "db.sqlite")


def publish(service, title, content, terms="", scope="project"):
    record = service.publish(title=title, content=content, search_terms=terms,
                             scope=scope, knowledge_type="procedure", evidence_summary="test fixture")
    key = record["knowledge"]["id"]
    service.store.feedback(agent_id="claude-code", knowledge_id=key, outcome="verified",
                           evidence_summary="fixture verified", evidence_kind="test", evidence_ref="fixture/test")
    return key


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 20])
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("expansion", [False, True])
def test_default_policy_matches_oracle_across_limits_and_lifecycle(service, limit, retired, expansion):
    fixtures = [
        ("backoff procedure", "clb-v2 token present", ""),
        ("backoff procedure second", "clb-v2 alternative", ""),
        ("backoff procedure notes", "neighbour without identifier", ""),
        ("nova datum alpha", "source-vocabulary first", "target-vocabulary"),
        ("nova datum beta", "source-vocabulary second", "target-vocabulary"),
        ("nova datum gamma", "target-vocabulary only", ""),
        ("an archived article", "clb-v2 exact archived", ""),
        ("quarantined article", "clb-v2 forbidden", ""),
        ("symbols +++", "punctuation +++ retained", ""),
    ]
    keys = [publish(service, *r) for r in fixtures]
    service.store.transit(keys[6], to_status="archived", reason="fixture", actor="test")
    service.store.transit(keys[7], to_status="quarantined", reason="fixture", actor="test")
    other = KnowledgeBridgeService(agent_id="codex", project_key="other", database_path=service.store.database_path)
    foreign = publish(other, "clb-v2 foreign private", "not for p")
    shared = publish(other, "clb-v2 user preference", "shared with p", scope="user")
    legacy = LegacyKnowledgeStore(service.store.database_path)
    for query in ("clb-v2", "source-vocabulary", "nova", "+++", "zzzzunrelated"):
        args = dict(requester_agent="codex", project_key="p", query=query, limit=limit,
                    include_retired=retired, expand_siblings=expansion)
        before, after = legacy.search(**args), service.store.search(**args)
        signature = lambda result: [(r["id"], r["origin"], r.get("related_to")) for r in result["results"]]
        assert signature(after) == signature(before)
        assert keys[7] not in [r["id"] for r in after["results"]]
        assert foreign not in [r["id"] for r in after["results"]]
        if not expansion:
            assert after["retrieval_diagnostics"]["enabled_stages"] == []
    # User scope remains visible across projects.
    result = service.store.search(requester_agent="codex", project_key="p", query="user preference", limit=20)
    assert shared in [r["id"] for r in result["results"]]


def test_parent_title_and_context_budget_are_auditable(tmp_path):
    reuse = ReuseStore(tmp_path / "db.sqlite")
    record = dict(id="test-child", title="Relay policy", content="Use the relay.",
                  source_agent="claude-code", origin="sibling", related_to="missing-parent",
                  related_title="The actual paper", provenance=[{"origin": "sibling", "depth": 2}])
    args = dict(agent_id="codex", project_key="p", session_id="test", prompt="relay",
                records=[record], retrieval_ms=1)
    _, context_text, emitted = reuse.start(**args, turn_id="one", budget=4000)
    assert "topic=The actual paper" in context_text
    assert emitted == ["test-child"]
    _, context_text, emitted = reuse.start(**args, turn_id="two", budget=10)
    assert (context_text, emitted) == ("", [])
    trace = reuse.existing("codex", "p", "test", "two")
    item = json.loads(trace["items_json"])[0]
    assert item["omitted_reason"] == "context_budget"
    assert item["provenance"] == record["provenance"]
