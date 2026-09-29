"""Small, model-free admission checks for retrieval evidence.

The checks here deliberately do not try to answer a question.  They only
prevent a record from being injected when its declared subject contradicts the
query or when the lexical match contains no question-specific evidence.
Records without subject metadata keep the legacy behaviour for compatibility.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Mapping
from agent_knowledge_bridge.retrieval_terms import concepts, normalize_word


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "with", "from", "that", "this",
    "what", "when", "where", "who", "why", "how", "did", "does", "do", "is",
    "are", "was", "were", "will", "would", "could", "can", "has", "have",
    "had", "her", "his", "their", "they", "them", "she", "he", "it", "about",
    "请", "帮我", "一下", "什么", "哪个", "哪些", "怎么", "如何", "为什么", "是否",
    "有没有", "是", "的", "了", "吗", "呢", "和", "与", "跟", "关于", "可以",
})
# Do not absorb sentence punctuation into Latin terms.  A query ending in
# ``procedure.`` must still match the stored term ``procedure`` while keeping
# identifiers such as ``project_key`` and ``clb-v2`` intact.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.:-][A-Za-z0-9]+)*|[一-鿿]{2,}")
_ANSWER_STOPWORDS = _STOPWORDS | frozenset({
    "after", "before", "during", "about", "into", "from", "with", "for",
    "did", "does", "do", "has", "have", "had", "was", "were", "will",
    "would", "could", "can", "should", "their", "her", "his", "they",
    "them", "she", "he", "it", "what", "which", "when", "where", "who",
    "why", "how", "much", "many", "kind", "type", "sort", "please",
})
_RELATION_ALIASES = {
    "realize": {"realize", "realized", "realizes", "realizing", "understand", "understood", "learn", "learned", "think", "thought"},
    "plan": {"plan", "plans", "planned", "planning", "intend", "intends", "intended", "hope", "hopes", "hoping"},
    "choose": {"choose", "chooses", "chose", "chosen", "select", "selected", "decide", "decided"},
    "symbolize": {"symbolize", "symbolizes", "symbolized", "represent", "represents", "represented", "mean", "means", "meant"},
    "excite": {"excite", "excited", "exciting", "look", "forward"},
    "motivate": {"motivate", "motivated", "motivation", "inspire", "inspired", "reason"},
    "research": {"research", "researched", "study", "studied", "investigate", "investigated"},
    "pursue": {"pursue", "pursued", "pursuing", "career", "field", "study", "studies"},
    "support": {"support", "supports", "supported", "serve", "serves", "served", "help", "helps"},
    "gift": {"gift", "gave", "give", "given", "present", "presented"},
    "do": {"do", "does", "did", "make", "made", "take", "took", "spend", "spent"},
}


def _relation_aliases(predicate: str) -> set[str]:
    aliases = set(_RELATION_ALIASES.get(predicate, {predicate}))
    if predicate.endswith("y") and len(predicate) > 3:
        aliases.add(predicate[:-1] + "ied")
    if predicate.endswith("e"):
        aliases.add(predicate + "d")
    else:
        aliases.update({predicate + "ed", predicate + "ing", predicate + "s"})
    return aliases


def _value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


@lru_cache(maxsize=4096)
def _terms(value: str) -> frozenset[str]:
    result: set[str] = set()
    for match in _WORD.findall(value or ""):
        value = normalize_word(match)
        if value in _STOPWORDS or value.isdigit():
            continue
        result.add(value)
        if re.fullmatch(r"[一-鿿]+", value) and len(value) > 2:
            result.update(value[i:i + 2] for i in range(len(value) - 1))
    return frozenset(result)


def _query_terms(value: str) -> frozenset[str]:
    return _terms(value)


def _merged_cjk_matches(
    query: str, matched_terms: set[str] | frozenset[str]
) -> list[list[int]]:
    """Return overlapping CJK matches as contiguous query intervals."""
    intervals: list[tuple[int, int]] = []
    for term in matched_terms:
        if not re.fullmatch(r"[一-鿿]+", term):
            continue
        start = 0
        while (index := query.find(term, start)) >= 0:
            intervals.append((index, index + len(term)))
            start = index + 1
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _independent_match_count(query: str, matched_terms: set[str] | frozenset[str]) -> int:
    """Count lexical evidence without treating overlapping CJK grams as separate.

    Sliding 2-grams are useful for FTS matching, but ``第三`` and ``三方`` are
    two views of the same ``第三方`` fragment.  Only overlapping intervals are
    merged; adjacent concepts such as ``数据库`` and ``并发`` remain independent.
    """
    latin = {term for term in matched_terms if not re.fullmatch(r"[一-鿿]+", term)}
    merged = _merged_cjk_matches(query, matched_terms)
    return len(latin) + len(merged)


def _has_distinctive_cjk_phrase(
    query: str, matched_terms: set[str] | frozenset[str]
) -> bool:
    """Four contiguous matched characters identify a phrase, even via 2-grams."""
    return any(end - start >= 4 for start, end in _merged_cjk_matches(query, matched_terms))


def normalize_subject_terms(value: Any) -> tuple[str, ...]:
    """Normalize the optional subject metadata without trusting its shape."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return ()
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                value = decoded
            else:
                value = re.split(r"[,;|]", value)
        except (TypeError, ValueError):
            value = re.split(r"[,;|]", value)
    if not isinstance(value, (list, tuple, set)):
        return ()
    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item and len(item) <= 100 and item.casefold() not in result:
            result.append(item.casefold())
    return tuple(result[:16])


def _subject_occurs(subject: str, query: str) -> bool:
    subject = subject.casefold().strip()
    query = query.casefold()
    if not subject:
        return False
    if re.fullmatch(r"[a-z0-9_.:-]+", subject):
        return re.search(r"(?<![a-z0-9_.:-])" + re.escape(subject) + r"(?![a-z0-9_.:-])", query) is not None
    return subject in query


def explicit_query_subjects(query: str, subjects: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(subject for subject in subjects if _subject_occurs(subject, query))


def subject_consistency(query: str, row: Mapping[str, Any]) -> tuple[bool, str, tuple[str, ...]]:
    subjects = normalize_subject_terms(_value(row, "subject_terms"))
    if not subjects:
        return True, "no_subject_metadata", ()
    requested = explicit_query_subjects(query, subjects)
    if requested:
        return True, "subject_match", requested
    # A subject-aware row may be safely served for an implicit question only
    # when the query has no other declared subject. We cannot infer a subject
    # from arbitrary prose, so an explicit mismatch is only reported when the
    # query names one of the known subjects in the same corpus. The caller
    # supplies that corpus-level hint through `subject_vocabulary`.
    return True, "subject_unresolved", ()


def subject_decision(
    query: str,
    row: Mapping[str, Any],
    *,
    subject_vocabulary: tuple[str, ...] = (),
) -> tuple[bool, str, tuple[str, ...]]:
    subjects = normalize_subject_terms(_value(row, "subject_terms"))
    if not subjects:
        return True, "no_subject_metadata", ()
    requested = tuple(subject for subject in subject_vocabulary if _subject_occurs(subject, query))
    if not requested:
        return True, "subject_unresolved", ()
    if set(requested) & set(subjects):
        return True, "subject_match", tuple(sorted(set(requested) & set(subjects)))
    return False, "subject_mismatch", requested


def evidence_support(query: str, row: Mapping[str, Any]) -> tuple[bool, str, float]:
    """Return a bounded lexical evidence score and a diagnostic reason."""
    query_terms = _query_terms(query)
    if not query_terms:
        return False, "generic_term_only", 0.0
    title_terms = _terms(f"{_value(row, 'title')} {_value(row, 'search_terms')}")
    strong = query_terms & title_terms
    strong_units = _independent_match_count(query, strong)
    # A title/search-term hit is a direct assertion of applicability. A body
    # hit is enough when it is distinctive; two independent body terms protect
    # against a single common word dragging an unrelated record in.
    if strong_units >= 2 or _has_distinctive_cjk_phrase(query, strong):
        return True, "topic_match", min(1.0, 0.60 + 0.10 * strong_units)
    # Body inspection is the expensive fallback. Bound it because a record's
    # title/search terms already carry the intended retrieval vocabulary and a
    # long transcript must not turn every query into a corpus scan.
    body_terms = _terms(str(_value(row, "content"))[:2400])
    body = query_terms & body_terms
    body_units = _independent_match_count(query, body)
    combined_units = _independent_match_count(query, body | strong)
    if (body_units >= 2 or _has_distinctive_cjk_phrase(query, body)
            or (strong_units and combined_units > strong_units)):
        return True, "multi_term_evidence", min(1.0, 0.45 + 0.08 * body_units)
    if len(body) == 1 and len(query_terms) <= 1:
        return True, "single_distinctive_term", 0.40
    # Two equivalent concepts can bridge languages without treating translated
    # synonyms as multiple independent evidence hits. One exact identifier plus
    # a different concept is also enough (SQLite + write, for example).
    body_text = str(_value(row, 'content'))[:2400]
    shared_concepts = concepts(query) & concepts(body_text)
    independent_literals = {term for term in body | strong if not concepts(term)}
    if len(shared_concepts) >= 2 or (shared_concepts and independent_literals):
        return True, 'bilingual_evidence', min(0.85, 0.50 + 0.10 * len(shared_concepts))
    if body or strong:
        return False, "insufficient_evidence", 0.20
    return False, "generic_term_only", 0.0


_NUMBER = re.compile(
    r'(?<![A-Za-z0-9_.-])\d+(?:\.\d+)?(?:\s*(?:%|ms|seconds?|minutes?|hours?|days?|MB|GB|毫秒|秒|分钟|小时|天|次))?'
    r'|[一二三四五六七八九十百千万两]+(?:毫秒|秒|分钟|小时|天|次|条)'
    r'|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:seconds?|minutes?|hours?|days?|retries)\b', re.I)
_NUMERIC_REQUEST = re.compile(
    r'\b(?:what|which|how (?:many|much|long))\b.{0,140}\b(?:numeric|number|threshold|timeout|limit|retention|ttl|duration|retries|size)\b'
    r'|(?:阈值|超时|上限|期限|时长|次数|容量|数值|数量).{0,24}(?:多少|几|具体值|具体数值)'
    r'|(?:具体|精确|明确).{0,12}(?:阈值|数值|数量)|\bexact (?:value|number)\b', re.I)
_MEASURED_REQUEST = re.compile(
    r'\b(?:benchmark|experiment|measured|measurement)\b.{0,45}\b(?:result|prove|show|improv|gain|success|accuracy)'
    r'|(?:哪|什么).{0,12}(?:实测|评测|实验).{0,16}(?:结果|证明|提升)'
    r'|(?:实测|评测|实验).{0,16}(?:提升|成功率|准确率|召回率).{0,10}(?:多少|如何)', re.I)
_MEASURED_EVIDENCE = re.compile(
    r'\b(?:measured|observed|achieved|scored|results? (?:show|were|was)|benchmark(?:ed) (?:at|result))\b'
    r'|(?:测得|实测|测试结果|评测结果|实验结果|观测到)', re.I)
_UNCOMMITTED = re.compile(
    r'\b(?:if|hypothetical|example|proposed|suggested|unverified|not yet|no (?:decision|measurement|result)|never measured)\b'
    r'|(?:假设|如果|举例|示例|建议值|尚未|未验证|未测量|未决定|没有.{0,8}(?:决定|实测|结果))', re.I)
_DISCUSSION_ONLY = re.compile(
    r'\b(?:only (?:a )?discussion|no (?:separate |additional )?(?:decision|rule|measured result).{0,24}(?:recorded|given)|gives no (?:additional )?rule)\b'
    r'|(?:仅讨论|只是讨论|尚未形成.{0,8}(?:规则|决定)|没有记录.{0,8}(?:结论|决定))', re.I)
_VALUE_FACETS = (
    (r'阈值|\bthresholds?\b', r'阈值|\bthresholds?\b'),
    (r'超时|\btimeout\b', r'超时|\btimeout\b'),
    (r'保留|期限|\bretention\b|\bttl\b', r'保留|期限|\bretention\b|\bttl\b'),
    (r'重试|\bretr(?:y|ies)\b', r'重试|\bretr(?:y|ies)\b'),
    (r'上限|\blimit\b', r'上限|\blimit\b|\bmaximum\b'),
)


@lru_cache(maxsize=512)
def _answer_requirement(query: str):
    from agent_knowledge_bridge.decisions import instruction_text
    instructions = instruction_text(query)
    if _MEASURED_REQUEST.search(instructions):
        return 'measurement'
    if _NUMERIC_REQUEST.search(instructions):
        return 'numeric'
    if re.search(
        r'\bhow (?:should|do|can)\b|\bwhat (?:makes|must|should)\b'
        r'|\bwhich\b.{0,40}\b(?:need|needs|require|requires|required)\b'
        r'|\bshould\b|\b(?:guidance|procedure|safeguards?|rules?)\b'
        r'|怎么|如何|应该|约定|规则',
        instructions,
        re.I,
    ):
        return 'guidance'
    return None


def answer_contract_rejection(query: str, row: Mapping[str, Any]) -> str | None:
    """Require the requested kind of evidence, including on legacy records.

    Index labels and search aliases cannot supply an answer value. This is a
    bounded lexical guard, not a semantic verifier of quantitative claims.
    """
    requirement = _answer_requirement(query)
    if requirement is None:
        return None
    content = str(_value(row, 'content'))[:5000]
    units = [unit.strip() for unit in re.split(r'[。！？\n]|(?<=[.!?])\s+', content) if unit.strip()]
    if requirement == 'guidance':
        return 'discussion_without_guidance' if units and all(_DISCUSSION_ONLY.search(unit) for unit in units) else None
    facets = [evidence for pattern, evidence in _VALUE_FACETS if re.search(pattern, query, re.I)]
    for unit in units:
        if _UNCOMMITTED.search(unit) or '?' in unit or '？' in unit or not _NUMBER.search(unit):
            continue
        if not evidence_support(query, {'title': '', 'search_terms': '', 'content': unit})[0]:
            continue
        if requirement == 'numeric' and all(re.search(facet, unit, re.I) for facet in facets):
            return None
        if requirement == 'measurement' and _MEASURED_EVIDENCE.search(unit):
            return None
    return 'missing_measured_answer' if requirement == 'measurement' else 'missing_numeric_answer'


def _canonical_relation(term: str) -> str | None:
    """Return a known relation family without guessing arbitrary verbs."""
    term = term.casefold()
    for canonical, aliases in _RELATION_ALIASES.items():
        if term in aliases or term in _relation_aliases(canonical):
            return canonical
    return None


def answer_slots(query: str, subject_vocabulary: tuple[str, ...]) -> dict[str, Any] | None:
    """Extract a conservative predicate/object contract from a question.

    A subject is useful when the query names one, but it is not mandatory. For
    subjectless questions we only create a gate when a relation family and an
    object cue are both recognizable; uncertain language keeps legacy behavior.
    """
    query_terms = _terms(query)
    subject_candidates = explicit_query_subjects(query, subject_vocabulary)
    query_tokens = list(_WORD.finditer(query))
    raw_tokens = [match.group(0).casefold() for match in query_tokens]
    filtered_tokens = [
        (index, token, match)
        for index, (token, match) in enumerate(zip(raw_tokens, query_tokens))
        if token not in _ANSWER_STOPWORDS and not token.isdigit()
    ]
    relation_entry = next(
        ((index, token, match) for index, token, match in filtered_tokens
         if _canonical_relation(token)),
        None,
    )
    relation_start = relation_entry[2].start() if relation_entry else len(query)
    # A known term after the relation is normally an object (for example,
    # SQLite in "what did the project choose about SQLite"), not the subject.
    subject = max(
        (candidate for candidate in subject_candidates
         if (match := re.search(re.escape(candidate), query, re.I))
         and match.start() < relation_start),
        key=len,
        default=None,
    )
    if subject:
        match = re.search(re.escape(subject), query, re.I)
        tail = query[match.end():] if match else query
    else:
        tail = query
    raw = [term.casefold() for term in _WORD.findall(tail)]
    words = [word for word in raw if word not in _ANSWER_STOPWORDS and not word.isdigit()]
    if not words:
        return None
    relation_index = next(
        (index for index, word in enumerate(words) if _canonical_relation(word)),
        None,
    )
    if relation_index is None:
        return None
    predicate = words[relation_index]
    canonical = _canonical_relation(predicate)
    aliases = set(_RELATION_ALIASES[canonical]) if canonical else _relation_aliases(predicate)
    aliases.update(_relation_aliases(canonical or predicate))
    objects = set(words[relation_index + 1:])
    objects.discard(predicate)
    if re.fullmatch(r"[一-鿿]+", predicate):
        objects |= set(query_terms) - set(_terms(subject or "")) - set(_terms(predicate))
    if not objects:
        return None
    return {"subject": subject, "predicate": predicate,
            "predicate_aliases": sorted(aliases), "object_terms": sorted(objects)}


def answerability_decision(query: str, candidates, *, subject_vocabulary=()):
    """Require query subject, relation and an object cue in evidence.

    Candidates may jointly cover object cues (multi-hop). Predicate support is
    mandatory because topical overlap alone is not evidence that answers the
    question. Queries without an explicit known subject retain the prior policy.
    """
    slots = answer_slots(query, tuple(subject_vocabulary))
    if slots is None:
        return True, "answer_slots_unresolved", {"required": []}
    structured = [
        candidate for candidate in candidates
        if len(normalize_subject_terms(_value(candidate.row, "subject_terms"))) == 1
    ]
    if not structured:
        return True, "answer_slots_unresolved", {
            "required": [], "reason": "no_single_subject_evidence"
        }
    # Prefer one evidence unit that contains the subject, predicate and object
    # together. This blocks topic-only matches such as a record mentioning a
    # charity race without stating what the subject realized.
    aliases = set(slots["predicate_aliases"])
    object_terms = set(slots["object_terms"])
    for candidate in structured:
        text = " ".join(
            str(_value(candidate.row, key))
            for key in ("title", "search_terms", "content")
        )[:5000]
        terms = _terms(text)
        if aliases & terms and (not object_terms or object_terms & terms):
            return True, "supported", {
                "subject": slots["subject"],
                "predicate": slots["predicate"],
                "object_terms": slots["object_terms"],
                "object_hits": sorted(object_terms & terms),
                "evidence_units": 1,
                "supporting_ids": [candidate.id],
            }
    texts = [
        " ".join(str(_value(c.row, key)) for key in ("title", "search_terms", "content"))[:5000]
        for c in structured
    ]
    evidence_terms = _terms(" ".join(texts))
    predicate_hit = bool(aliases & evidence_terms)
    object_hits = sorted(object_terms & evidence_terms)
    object_ok = not object_terms or bool(object_hits)
    # Multi-hop is allowed only when the records are from one source session;
    # unrelated records from the same project cannot jointly manufacture an
    # answer.
    sessions = {
        str(_value(candidate.row, "source_session")).rsplit(":", 1)[0]
        for candidate in structured
        if _value(candidate.row, "source_session")
    }
    ok = predicate_hit and object_ok and (not sessions or len(sessions) == 1)
    missing = []
    if not predicate_hit:
        missing.append("predicate")
    if not object_ok:
        missing.append("object")
    supporting_ids = [
        candidate.id for candidate in structured
        if (aliases & _terms(" ".join(
            str(_value(candidate.row, key))
            for key in ("title", "search_terms", "content")
        )[:5000])) or (object_terms & _terms(" ".join(
            str(_value(candidate.row, key))
            for key in ("title", "search_terms", "content")
        )[:5000]))
    ]
    return ok, "supported" if ok else "insufficient_answer_evidence", {
        "subject": slots["subject"], "predicate": slots["predicate"],
        "object_terms": slots["object_terms"], "object_hits": object_hits,
        "missing": missing, "evidence_units": len(structured),
        "supporting_ids": supporting_ids,
    }


def retrieval_decision(
    query: str,
    row: Mapping[str, Any],
    *,
    subject_vocabulary: tuple[str, ...] = (),
) -> tuple[bool, str, dict[str, Any]]:
    subject_ok, subject_reason, requested = subject_decision(
        query, row, subject_vocabulary=subject_vocabulary
    )
    if not subject_ok:
        return False, subject_reason, {"requested_subjects": list(requested)}
    supported, support_reason, score = evidence_support(query, row)
    if not supported:
        # Lexical evidence is intentionally advisory at this layer. A single
        # turn can carry one half of a multi-hop answer, so hard rejecting it
        # causes large recall regressions. Subject contradiction is the hard
        # safety boundary; weak evidence remains visible with a diagnostic for
        # a later answer-level verifier.
        return True, "weak_evidence", {
            "subject_reason": subject_reason,
            "requested_subjects": list(requested),
            "support_score": score,
            "support_reason": support_reason,
        }
    return True, "supported", {
        "subject_reason": subject_reason,
        "requested_subjects": list(requested),
        "support_reason": support_reason,
        "support_score": score,
    }


def filter_retrieval_candidates(candidates, query: str, *, subject_vocabulary=()):
    """Filter Candidate objects and return audit-friendly omissions."""
    accepted = []
    omitted = []
    for candidate in candidates:
        contract_rejection = answer_contract_rejection(query, candidate.row)
        if contract_rejection:
            omitted.append({'knowledge_id': candidate.id, 'reason': contract_rejection,
                            'origin': candidate.origin})
            continue
        # Legacy records have no trustworthy speaker/subject boundary, so they
        # cannot use the subject gate. They still need a minimum evidence
        # contract: for a multi-term query, one generic word (HTTP, SSH, JSON)
        # is not enough to inject a record. This closes the old compatibility
        # escape hatch while retaining exact/single-term lookups.
        if not normalize_subject_terms(_value(candidate.row, "subject_terms")):
            query_terms = _query_terms(query)
            supported, reason, score = evidence_support(query, candidate.row)
            # A vocabulary bridge/sibling carries an explicit graph
            # relationship and may inherit the seed's evidence. An anchor is
            # only a rare-term expansion, so it must still satisfy the local
            # evidence floor; otherwise one shared word can re-enter through
            # the expansion path after being correctly omitted as a direct hit.
            if supported or len(query_terms) <= 1 or candidate.origin in {"bridged", "sibling"}:
                accepted.append(candidate)
            else:
                omitted.append({
                    "knowledge_id": candidate.id,
                    "reason": reason,
                    "details": {"support_score": score, "legacy_record": True},
                    "origin": candidate.origin,
                })
            continue
        # Expanded rows inherit evidence from the direct/bridge seed. Requiring
        # a literal query term on a sibling would defeat the vocabulary bridge
        # itself. Subject consistency still applies to every route.
        ok, reason, details = retrieval_decision(
            query, candidate.row, subject_vocabulary=tuple(subject_vocabulary)
        )
        if ok and candidate.origin != "direct":
            details = {"inherited_from": candidate.origin, **details}
        elif not ok and candidate.origin != "direct" and reason in {
            "generic_term_only", "insufficient_evidence"
        }:
            ok, reason = True, "inherited_evidence"
            details = {"inherited_from": candidate.origin, **details}
        if ok:
            accepted.append(candidate)
        else:
            omitted.append({
                "knowledge_id": candidate.id,
                "reason": reason,
                "details": details,
                "origin": candidate.origin,
            })
    return tuple(accepted), omitted


def apply_answerability_gate(candidates, query: str, *, subject_vocabulary=()):
    """Drop a page only when no candidate set can satisfy answer slots."""
    accepted, reason, details = answerability_decision(
        query, candidates, subject_vocabulary=subject_vocabulary
    )
    if accepted:
        supporting_ids = set(details.get("supporting_ids") or [])
        if supporting_ids:
            # Once a page has enough evidence, do not inject every topical
            # neighbour. Legacy rows without subject metadata remain visible;
            # structured rows are reduced to the units that supplied a
            # predicate/object cue (or the same-session multi-hop evidence).
            candidates = tuple(
                candidate for candidate in candidates
                if not normalize_subject_terms(_value(candidate.row, "subject_terms"))
                or candidate.id in supporting_ids
            )
        return tuple(candidates), []
    omitted = [
        {
            "knowledge_id": candidate.id,
            "reason": reason,
            "details": details,
            "origin": candidate.origin,
        }
        for candidate in candidates
    ]
    return (), omitted
