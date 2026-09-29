"""Regression tests for bilingual recall and conservative admission."""

import pytest

from agent_knowledge_bridge.service import KnowledgeBridgeService


def active(service, title, content):
    created = service.publish(
        title=title,
        content=content,
        knowledge_type="fact",
        evidence_summary="retrieval fixture",
    )
    key = created["knowledge"]["id"]
    service.store.feedback(
        agent_id=service.agent_id,
        knowledge_id=key,
        outcome="verified",
        evidence_kind="test",
        evidence_ref="tests/retrieval-relevance.json",
        evidence_summary="fixture verified",
    )
    return key


def test_chinese_rewrite_uses_the_lightweight_alias_bridge(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    sqlite = active(
        service,
        "SQLite concurrency",
        "Use WAL journal_mode for SQLite concurrent readers.",
    )
    result = service.search("数据库并发读写怎么处理", limit=3)
    assert [item["id"] for item in result["results"]] == [sqlite]


@pytest.mark.parametrize(("query", "title", "content"), [
    (
        "对话内容发给云端模型前必须做什么？",
        "Remote model handling",
        "Redact credentials before sending conversation excerpts to a remote model provider.",
    ),
    (
        "Which values must never be stored as reusable knowledge?",
        "知识持久化边界",
        "知识记录中不得保存 API Key、Bearer Token 或明文密码。",
    ),
    (
        "一次普通使用反馈能让归档记录复活吗？",
        "Archived knowledge lifecycle",
        "A normal used signal must not reactivate archived knowledge by itself.",
    ),
    (
        "How should the previous value behave after a rule is superseded?",
        "规则版本历史",
        "被替代版本可用于明确的历史查询，但不能当成当前事实。",
    ),
])
def test_governance_glossary_bridges_languages(tmp_path, query, title, content):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(service, title, content)
    result = service.search(query, limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


def test_one_common_protocol_word_does_not_answer_an_external_history_question(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    active(
        service,
        "HTTP timeout handling",
        "Set connect and read timeouts for HTTP requests.",
    )
    result = service.search("What is the HTTP history?", limit=3)
    assert result["results"] == []


def test_distinctive_technical_identifier_survives_a_complex_question(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(
        service,
        "Native hook boundary",
        "MCP is optional; native hooks perform automatic recall without model-initiated tool calls.",
    )
    result = service.search("自动记忆召回是否必须依赖 MCP？", limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


@pytest.mark.parametrize(("query", "content"), [
    (
        "项目专属记忆如何隔离？",
        "Project-scoped knowledge is visible only inside the mapped project.",
    ),
    (
        "How should paraphrases be separated between development and test?",
        "改写题必须按底层事实分组切分数据集，不能跨开发集和测试集。",
    ),
    (
        "额外延迟应该怎样做公平对比？",
        "Measure incremental latency with framework-on and framework-off paired runs.",
    ),
])
def test_phrase_bounded_technical_concepts_bridge_languages(tmp_path, query, content):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(service, "Technical guidance", content)
    result = service.search(query, limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


def test_one_phrase_bounded_concept_can_carry_cross_language_recall(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(
        service,
        "Frequency placement",
        "Document-frequency measurements belong on writes or behind write-invalidated caches.",
    )
    result = service.search("较重的文档频率统计应该放在哪条路径？", limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


def test_phrase_bounded_concept_does_not_rescue_a_discussion_only_note(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    active(
        service,
        "Frequency discussion",
        "Document frequency is only a discussion topic; no separate rule was recorded.",
    )
    result = service.search("文档频率统计应该放在哪条路径？", limit=3)
    assert result["results"] == []


def test_incomplete_transcript_question_uses_evidence_state_not_generic_words(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(
        service,
        "Adapter evidence boundary",
        "When the adapter cannot inspect the complete turn, mark evidence as unknown rather than claiming full coverage.",
    )
    result = service.search("How should incomplete transcript visibility be represented?", limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


def test_direct_candidates_need_two_independent_terms_for_multi_term_queries(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    dpi = active(
        service,
        "DPI replay acceptance",
        "Validate DPI with positive PCAP hits and negative PCAP false positives.",
    )
    ssh = active(
        service,
        "SSH direction fields",
        "SSH req fields describe client requests; rsp fields describe server responses. Match replay evidence.",
    )
    result = service.search("DPI PCAP replay", limit=3)
    ids = [item["id"] for item in result["results"]]
    assert dpi in ids
    assert ssh not in ids


@pytest.mark.parametrize(("query", "answer", "decoy"), [
    (
        "What safeguards are required for database migrations?",
        "Run schema changes inside an explicit transaction and make migrations idempotent.",
        "For database, this note is only a discussion topic and gives no additional rule.",
    ),
    (
        "What makes a useful retrieval anchor?",
        "Use rare, discriminative terms as anchors; common topic words alone are not enough.",
        "For retrieval, this note is only a discussion topic and gives no additional rule.",
    ),
    (
        "What must evidence cover before it is injected as an answer?",
        "Answerability requires matching the requested subject and relation, not just the general topic.",
        "For retrieval, this note is only a discussion topic and gives no additional rule.",
    ),
])
def test_guidance_queries_reject_topic_only_discussion(tmp_path, query, answer, decoy):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    expected = active(service, "Agreed guidance", answer)
    active(service, "Discussion note", decoy)
    result = service.search(query, limit=3)
    assert [item["id"] for item in result["results"]] == [expected]


def test_which_need_guidance_rejects_discussion_only_note(tmp_path):
    service = KnowledgeBridgeService(
        agent_id="codex", project_key="relevance", database_path=tmp_path / "db.sqlite"
    )
    active(
        service,
        "Discussion note",
        "The team discussed governance; no separate decision was recorded in this note.",
    )
    result = service.search(
        "Which stages need separate evidence in a reuse claim?",
        limit=3,
    )
    assert result["results"] == []
