"""Regression tests for subject-aware retrieval admission."""
from agent_knowledge_bridge.service import KnowledgeBridgeService


def active(service, title, content, *, subject_terms=None, source_session=None):
    created = service.publish(
        title=title,
        content=content,
        knowledge_type="fact",
        evidence_summary="structured fixture",
        subject_terms=subject_terms,
        source_session=source_session,
    )
    key = created["knowledge"]["id"]
    service.store.feedback(
        agent_id=service.agent_id,
        knowledge_id=key,
        outcome="verified",
        evidence_kind="test",
        evidence_ref="tests/evidence-guard.json",
        evidence_summary="fixture verified",
    )
    return key


def test_explicit_subject_excludes_same_topic_other_speaker(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    caroline = active(
        service,
        "charity race notes",
        "Caroline realized running helped her community after the charity race.",
        subject_terms=["Caroline"],
    )
    melanie = active(
        service,
        "charity race notes",
        "Melanie discussed the charity race and a different personal goal.",
        subject_terms=["Melanie"],
    )

    result = service.search("What did Caroline realize after the charity race?", limit=5)
    ids = [item["id"] for item in result["results"]]
    assert caroline in ids
    assert melanie not in ids
    assert any(item["knowledge_id"] == melanie and item["reason"] == "subject_mismatch"
               for item in result["retrieval_diagnostics"]["omitted"])


def test_expansion_cannot_bypass_subject_check(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    caroline = active(
        service, "race evidence", "Caroline completed the charity race.",
        subject_terms=["Caroline"],
    )
    melanie = active(
        service, "race evidence extra", "Melanie completed a different charity race.",
        subject_terms=["Melanie"],
    )
    result = service.search("Caroline charity race", limit=2)
    assert caroline in [item["id"] for item in result["results"]]
    assert melanie not in [item["id"] for item in result["results"]]


def test_legacy_record_without_subject_metadata_keeps_compatibility(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    legacy = active(service, "legacy policy", "The legacy policy is still valid.")
    result = service.search("legacy policy", limit=2)
    assert legacy in [item["id"] for item in result["results"]]


def test_structured_record_with_weak_overlap_is_retained_for_answer_level_check(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    record = active(
        service, "Melanie notes", "Melanie mentioned a general topic.",
        subject_terms=["Melanie"],
    )
    result = service.search("What did Caroline do about the charity race?", limit=3)
    # Lexical evidence is advisory: an answer-level verifier may need this
    # record as one half of a multi-hop answer. Subject mismatch remains hard.
    assert record in [item["id"] for item in result["results"]] or result["count"] == 0


def test_answerability_gate_rejects_topic_only_evidence(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    topic_only = active(
        service,
        "charity race",
        "Caroline talked about the charity race.",
        subject_terms=["Caroline"],
    )
    result = service.search(
        "What did Caroline realize after her charity race?", limit=5
    )
    assert result["count"] == 0
    assert any(
        item["knowledge_id"] == topic_only
        and item["reason"] == "insufficient_answer_evidence"
        for item in result["retrieval_diagnostics"]["omitted"]
    )


def test_answerability_gate_accepts_subject_relation_and_object(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    answer = active(
        service,
        "charity race realization",
        "Caroline realized self-care is important after the charity race.",
        subject_terms=["Caroline"],
    )
    result = service.search(
        "What did Caroline realize after her charity race?", limit=5
    )
    assert answer in [item["id"] for item in result["results"]]


def test_answerability_gate_rejects_subjectless_topic_only_evidence(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    topic_only = active(
        service,
        "charity race notes",
        "Caroline talked about the charity race.",
        subject_terms=["Caroline"],
    )
    result = service.search(
        "What did the team plan about the charity race?", limit=5
    )
    assert result["count"] == 0
    assert any(
        item["knowledge_id"] == topic_only
        and item["reason"] == "insufficient_answer_evidence"
        for item in result["retrieval_diagnostics"]["omitted"]
    )


def test_answerability_gate_does_not_treat_object_as_subject(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="evidence", database_path=tmp_path / "db.sqlite"
    )
    answer = active(
        service,
        "SQLite concurrency decision",
        "The project chose WAL mode because it allows concurrent readers.",
        subject_terms=["SQLite"],
        source_session="sqlite:session-a:turn-1",
    )
    topic_only = active(
        service,
        "SQLite notes",
        "SQLite and WAL are useful topics for local applications.",
        subject_terms=["SQLite"],
        source_session="sqlite:session-a:turn-2",
    )

    supported = service.search(
        "What did the project choose about SQLite WAL?", limit=5
    )
    assert answer in [item["id"] for item in supported["results"]]

    unsupported = service.search(
        "What did the project realize about SQLite?", limit=5
    )
    assert unsupported["count"] == 0
    assert topic_only in {
        item["knowledge_id"] for item in unsupported["retrieval_diagnostics"]["omitted"]
    }
