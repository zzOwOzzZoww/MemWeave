from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from time import perf_counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from agent_knowledge_bridge.retrieval_stats import StatisticsSnapshot
from agent_knowledge_bridge.retrieval_terms import (
    WORD, concept_tokens, is_concept_token, normalize_word, query_aliases,
)
from agent_knowledge_bridge.experiences import filter_rows
from agent_knowledge_bridge.decisions import query_intent, filter_candidates, record_digest, obsolete, POLICY_VERSION
from agent_knowledge_bridge import knowledge_versions as versions
from agent_knowledge_bridge.retrieval_pipeline import (
    RetrievalPolicy, StageContext, expand, arbitrate, truncate,
)
from agent_knowledge_bridge.evidence import (
    normalize_subject_terms, filter_retrieval_candidates, apply_answerability_gate,
)


KNOWLEDGE_TYPES = {"fact", "preference", "procedure", "decision"}
SCOPES = {"project", "user"}
FEEDBACK_OUTCOMES = {"used", "verified", "rejected"}
REVIEW_STATUSES = {"candidate", "quarantined"}
# Full status set. `stale` still participates in retrieval but ranks below active
# records; `archived` is retired from retrieval entirely and is never deleted, so
# retirement stays reversible.
LIFECYCLE_STATUSES = {"candidate", "active", "stale", "archived", "quarantined"}
RETIRED_STATUSES = frozenset({"archived"})
#: How many lifecycle transitions to keep per record. The bound exists so a
#: record bouncing between `active` and `archived` cannot grow the bookkeeping
#: faster than the library it governs. Recent history is what an audit needs.
AUDIT_ROWS_PER_RECORD = 50
# Unreviewed proposals should not remain in the manual queue forever. The
# deadline is stored on the record so a later governance sweep is deterministic
# and old databases can be migrated without changing their creation time.
CANDIDATE_REVIEW_DAYS = 1
EVIDENCE_KINDS = {
    "observation",
    "test",
    "command",
    "user_approval",
    "artifact",
    "duplicate",
}
OBJECTIVE_EVIDENCE_KINDS = {"test", "command", "user_approval", "artifact"}
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Composite identifiers such as `ledger-svc` or `api_prefix` are the tokens that
# actually tell records apart. In a homogeneous corpus BM25 alone lets shared
# wording drown them out, so these are handled as an explicit relevance signal.
DISCRIMINATIVE_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-._:][A-Za-z0-9]+)+")
PROJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
#: How many whole terms a record must share with the retrieved set before it
#: counts as being about the same subject. One shared term is a coincidence --
#: every record in a project mentions the same project nouns -- so two is the
#: smallest overlap that carries information. Measured against the real corpus,
#: a genuine cluster member shares five or more; the floor exists to exclude the
#: long tail, not to select the cluster.
MIN_SIBLING_OVERLAP = 2
#: Upper bound on records considered for sibling expansion. The pool is read in
#: one indexed query and scored in Python, so the cost is a single scan of at
#: most this many rows regardless of how large the project grows.
SIBLING_CANDIDATE_CAP = 400
#: How many siblings may be interleaved into one answer. Bounded so that a
#: subject with a very large cluster cannot fill the caller's whole budget with
#: supporting context and crowd out the answer to the question actually asked.
MAX_SIBLINGS = 3

# ---------------------------------------------------------------------------
# Query-side vocabulary bridging.
#
# Lexical retrieval fails across Agents for a reason no ranking tweak can fix:
# two Agents name the same thing differently. Claude writes "中转 endpoint" and
# Codex asks for "代理地址"; the records are about one subject and share no term.
# Retrieval is not misordering them -- they never enter the candidate pool.
#
# The bridge is built from the corpus itself. Every record already carries a
# session id and a project, so the records an Agent wrote in one sitting are a
# usable statement of "these belong together". Terms that recur across such
# neighbours are the variants, and recording that association costs one indexed
# read at write time and one indexed read at query time. No model, no vectors,
# and nothing added to the hot path until a query actually comes back thin.
# ---------------------------------------------------------------------------

#: A co-occurrence pair must be seen this often before it may rewrite a query.
#: One observation is an accident of phrasing; the floor is what keeps a single
#: unusual sentence from permanently redefining a common term.
MIN_COOCCURRENCE = 2
#: How many extension terms one query may gain. Bounded because every added term
#: weakens the match: under the fixed context budget an irrelevant record costs
#: more than a missing one, so this is deliberately a small number.
MAX_QUERY_EXTENSIONS = 3
#: The bridge fires only when the direct pass could not fill the slots the
#: caller asked for, which is the test ``len(rows) < limit`` inside ``search``.
#: Deliberately not a numeric floor of its own -- a floor is unreachable here. A
#: pair reaches ``MIN_COOCCURRENCE`` only when two records both assert it, and a
#: record can only assert a pair between terms *it itself contains*, so an edge
#: at strength two implies two records containing the query's own term. Those
#: records are direct hits, so the strength floor guarantees the very thing that
#: would close a floor-shaped gate, and the bridge is dead code for any project
#: holding two records that share a term. Measured on the real corpus at the
#: production limit of three: zero of sixty queries returned fewer than two
#: direct hits.
#:
#: Whether an answer came back *thin* is a different question from whether it
#: came back at all, and it is the one that varies. A full page has nothing to
#: gain from a restatement, so the second FTS pass is skipped and the hot path is
#: unchanged; a short page is the one case where a variant term can only add.
#: How far past the caller's ``limit`` a widened result set may extend. Direct
#: hits keep the slots; this only says how much may follow them. Bounded because
#: the fixed context budget makes a surplus record a real cost, and because a
#: caller that asked for three did not ask for ten.
BRIDGE_SLACK = 2
#: A term must occur in no more records than this to serve as an anchor for an
#: entity neighbourhood. Rarity is the property that makes a term identifying
#: rather than merely topical: ``论文`` occurs in most records and names a genre,
#: while ``第三方`` occurs in four and names a subject. Measured on the real
#: corpus, the anchors this admits are the ones a reader would name as subjects.
MAX_ANCHOR_DF = 6
#: Upper bound on records pulled by an expanded query. The extension pass is a
#: second FTS read, and its cost must not scale with the corpus.
EXPANSION_POOL = 24
#: Upper bound on stored co-occurrence pairs per term. Keeps the graph bounded by
#: vocabulary rather than by the number of sessions ever recorded.
MAX_EDGES_PER_TERM = 24

SEARCH_INDEX_VERSION = 'concept-markers-v3'


def cjk_ngrams(value: str) -> list[str]:
    """Small index-time/query-time terms without a tokenizer dependency."""
    result: list[str] = []
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        result.extend(span[index:index + 2] for index in range(len(span) - 1))
        result.extend(span[index:index + 3] for index in range(len(span) - 2))
    return list(dict.fromkeys(result))


def retrieval_tokens(query: str) -> list[str]:
    """Create bounded lexical and CJK n-gram terms while dropping numeric noise."""
    # Small, explicit colloquial rewrites keep the Core model-free while making
    # common references such as "按我平常那套" searchable against a stored
    # "页面主题" assertion. They are bounded vocabulary bridges, not a semantic
    # classifier; decision guards still decide whether a hit may be emitted.
    query = re.sub(r'视觉习惯|常用风格|页面颜色|清爽配色|默认视觉', '页面主题', query)
    markers = list(concept_tokens(query))
    aliases = query_aliases(query)
    raw = WORD.findall(query)
    words, grams = [], []
    for token in raw:
        if token.isdigit():
            continue
        words.extend((normalize_word(token), token))
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            grams.extend(cjk_ngrams(token))
    # Canonical markers are stable across languages and index versions. Keep
    # surface aliases as a compatibility path for databases not rebuilt yet.
    # Reserve space for both instead of letting long CJK n-grams consume the
    # entire MATCH budget.
    words = list(dict.fromkeys(words))
    return list(dict.fromkeys(words[:24] + markers + aliases + words[24:] + grams))[:64]


def indexed_content(title: str, content: str, search_terms: str) -> str:
    # Metadata-free legacy records still need searchable Chinese body terms.
    # Enrich on writes/reindex, never scan and tokenize the corpus on recall.
    text = f"{title} {search_terms} {content[:5000]}"
    aliases = " ".join(dict.fromkeys(
        list(concept_tokens(text)) + cjk_ngrams(text)[:1024]
        + [normalize_word(w) for w in WORD.findall(text)
                           if w.isascii() and normalize_word(w) != w.casefold()][:128]))
    return "\n".join(part for part in (content, search_terms, aliases) if part)


#: A whole word, as opposed to a CJK sliding-window fragment. ``retrieval_tokens``
#: expands a run of Han characters into every 2- and 3-gram it contains, which is
#: what lets a query match a corpus it shares no tokenizer with. Those fragments
#: are load-bearing for the MATCH, but they cannot be used to measure *how much*
#: two records are about the same thing: `文不`, `不披` and `中转第` are artefacts
#: of the window, not words, and a long span manufactures dozens of them.
WHOLE_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]+|[一-鿿]{2,}")
#: Latin terms that carry no topical information, so they cannot bridge records.
#: Kept deliberately short: an over-eager stop list removes the identifiers that
#: make this signal work in the first place.
BRIDGE_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "not", "are", "was",
        "its", "use", "using", "must", "only", "when", "then", "than", "into",
        "原文", "内容", "标题", "说明", "记录", "情况", "问题", "方式", "时候",
    }
)


def bounded_document_frequencies(connection, terms, *, ceiling):
    """Read live FTS document counts; counts above ceiling need not be exact.

    One batched query preserves MATCH phrase/tokenizer semantics, including
    composite identifiers. Each term stops after ceiling + 1 matching rows.
    No application cache can become stale when a second Agent writes, a record
    retires, or the index is rebuilt.
    """
    terms = list(dict.fromkeys(terms))
    if not terms:
        return {}
    expressions = [(term, f'"{term.replace(chr(34), chr(34) * 2)}"') for term in terms]
    try:
        values = ','.join('(?,?)' for _ in terms)
        params = [value for pair in expressions for value in pair]
        rows = connection.execute(
            f"WITH terms(term, expression) AS (VALUES {values}) "
            "SELECT term, (SELECT count(*) FROM (SELECT rowid FROM knowledge_fts "
            "WHERE knowledge_fts MATCH terms.expression LIMIT ?)) FROM terms",
            (*params, ceiling + 1),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}
    except sqlite3.OperationalError:
        # Retain per-term error isolation on hosts rejecting the batched query.
        counts = {}
    for term in terms:
        try:
            row = connection.execute(
                "SELECT count(*) FROM (SELECT rowid FROM knowledge_fts "
                "WHERE knowledge_fts MATCH ? LIMIT ?)",
                (f'"{term.replace(chr(34), chr(34) * 2)}"', ceiling + 1),
            ).fetchone()
            counts[term] = int(row[0]) if row else 0
        except sqlite3.OperationalError:
            continue
    return counts


def anchor_terms(connection, terms, *, max_df=MAX_ANCHOR_DF, max_anchors=3, statistics=None):
    """Select the query terms rare enough to name an entity.

    The discriminative pin solves the same problem for composite identifiers --
    ``ledger-svc`` against ``cache-svc``. Measured against the real corpus, that
    path never fires on this user's content: the corpus holds zero composite
    Latin identifiers, because the work is written in Chinese. A Han subject is
    carried by a whole term (``第三方``) rather than by a hyphenated name, so the
    property the pin actually needs is not the punctuation but the rarity.

    Rarity is what makes a term identifying rather than topical. ``论文`` occurs
    in most records and names a genre; ``第三方`` occurs in four and names a
    subject. The threshold is a document count and not a ratio, because the
    corpus is small and a ratio over fifteen records cannot distinguish one that
    appears in four from one that appears in twelve.

    Document frequency comes from a bounded, batched read of the live index.
    All terms, including composite identifiers, retain phrase MATCH semantics.
    Frequencies above the threshold are clipped because they cannot qualify.
    """
    candidates = [
        term for term in dict.fromkeys(terms)
        if len(term) >= 2 and not is_concept_token(term)
    ]
    if not candidates:
        return []
    found: list[tuple[int, int, str]] = []
    frequencies = (statistics.frequencies(candidates, ceiling=max_df) if statistics else
                   bounded_document_frequencies(connection, candidates, ceiling=max_df))
    for term, count in frequencies.items():
        if 0 < count <= max_df:
            found.append((count, -len(term), term))
    # Rarest first; the longer term breaks ties because it carries more of what
    # the caller meant, and the term itself breaks the rest so the choice is
    # deterministic rather than dependent on query order.
    found.sort()
    return [term for _count, _length, term in found[:max_anchors]]


def _whole_terms(value: str) -> set[str]:
    """Return the words in ``value``, never the n-gram fragments between them.

    Split a Han span on nothing and `第三方中转` arrives as one term while the
    query's `论文正文不披露中转第三方` arrives as another, so the two never equal
    each other even though they share `第三方`.  Counting character overlap is
    what repairs that, and it is why this returns both the span and its bigrams
    for CJK, but keeps Latin terms whole -- `third-party` and `relay` are already
    the right granularity and splitting them would only add noise.
    """
    terms: set[str] = set()
    for match in WHOLE_TERM.findall(value or ""):
        if match.isdigit() or match.lower() in BRIDGE_STOPWORDS:
            continue
        terms.add(match.lower())
        if re.fullmatch(r"[一-鿿]+", match) and len(match) > 2:
            # A long Han run is a phrase, not a term: index its bigrams so two
            # phrasings of the same idea meet.  3-grams are dropped on purpose --
            # they are rare enough that requiring one silently kills the bridge.
            terms.update(
                match[index:index + 2] for index in range(len(match) - 1)
            )
    return terms


#: Terms that may never bridge vocabularies: they are too common to carry an
#: association, so connecting them would rewrite every query into every other
#: query. Longer than ``BRIDGE_STOPWORDS`` because this list is about *edges*,
#: not about topicality, and a single CJK character is never a term here.
BRIDGE_STOPWORDS = BRIDGE_STOPWORDS | frozenset(
    {"ledger", "service", "policy", "note", "data", "file", "test", "code"}
)


def _bridge_terms(title: str, search_terms: str) -> set[str]:
    """Terms from one record that may take part in a vocabulary bridge.

    Only the title and the author-written ``search_terms`` are used, never the
    body. The body is prose and its words are mostly about the sentence rather
    than the subject; the title and the terms line are the author's own summary
    of what the record is about, which is exactly the signal wanted here.
    """
    terms = _whole_terms(f"{title} {search_terms}")
    # A single character cannot distinguish two subjects, and a term long enough
    # to be a whole sentence is a phrase rather than a word.
    return {
        term
        for term in terms
        if len(term) >= 2 and len(term) <= 24 and term not in BRIDGE_STOPWORDS
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeStore:
    def __init__(
        self, database_path: Path, *, clock: Callable[[], str] = utc_now
    ) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._subject_vocabulary_cache: dict[tuple[str, str], tuple[int, tuple[str, ...]]] = {}
        # Single source of time for everything the store writes. Lifecycle
        # decisions compare stored timestamps against "now", so the store and the
        # governor must read the same clock or a retirement rule becomes
        # untestable and, worse, unreproducible.
        self.clock = clock
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_records (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    knowledge_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    source_session TEXT,
                    evidence_speaker TEXT,
                    subject_terms TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL UNIQUE,
                    adopted_count INTEGER NOT NULL DEFAULT 0,
                    verified_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    candidate_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_evidence (
                    id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL REFERENCES knowledge_records(id),
                    agent_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL DEFAULT 'observation',
                    evidence_ref TEXT NOT NULL DEFAULT '',
                    status_before TEXT,
                    status_after TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_project_status
                    ON knowledge_records(project_key, status);
                CREATE INDEX IF NOT EXISTS idx_evidence_knowledge
                    ON knowledge_evidence(knowledge_id, created_at);

                CREATE TABLE IF NOT EXISTS agent_registry (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    adapter_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    installed INTEGER NOT NULL DEFAULT 0,
                    detected_by_json TEXT NOT NULL DEFAULT '[]',
                    executable_path TEXT,
                    config_path TEXT,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    first_registered_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_registry_enabled
                    ON agent_registry(enabled, updated_at);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    knowledge_id UNINDEXED,
                    title,
                    content,
                    tokenize='unicode61'
                );
                """
            )
            legacy_record_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_records)"
                ).fetchall()
            }
            self._ensure_column(connection, "knowledge_records", "source_session", "TEXT")
            self._ensure_column(connection, "knowledge_records", "evidence_speaker", "TEXT")
            self._ensure_column(
                connection, "knowledge_records", "subject_terms", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(
                connection, "knowledge_records", "search_terms", "TEXT NOT NULL DEFAULT ''"
            )
            self._initialize_retrieval_revision(connection)
            connection.execute('''CREATE TABLE IF NOT EXISTS experience_outcomes (
                knowledge_id TEXT PRIMARY KEY REFERENCES knowledge_records(id),
                admission TEXT NOT NULL, failed_event TEXT, passed_event TEXT,
                observed INTEGER NOT NULL DEFAULT 0, passed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, recovered INTEGER NOT NULL DEFAULT 0,
                unverified INTEGER NOT NULL DEFAULT 0, cross_agent_passed INTEGER NOT NULL DEFAULT 0,
                failure_events INTEGER NOT NULL DEFAULT 0, last_outcome TEXT,
                updated_at TEXT NOT NULL)''')
            self._ensure_column(
                connection,
                "knowledge_evidence",
                "evidence_kind",
                "TEXT NOT NULL DEFAULT 'observation'",
            )
            self._ensure_column(
                connection,
                "knowledge_evidence",
                "evidence_ref",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(connection, "knowledge_evidence", "status_before", "TEXT")
            self._ensure_column(connection, "knowledge_evidence", "status_after", "TEXT")
            # Lifecycle half-loop: without a hit trail there is no evidence a record
            # is still earning its place, so nothing can ever be retired. `hit_count`
            # counts injections into a caller's context, which is a weaker signal
            # than `adopted_count` (did the caller then use it?) and a stronger one
            # than mere retrieval.
            self._ensure_column(connection, "knowledge_records", "last_hit_at", "TEXT")
            self._ensure_column(
                connection, "knowledge_records", "hit_count", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                connection, "knowledge_records", "candidate_expires_at", "TEXT"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_audit (
                    id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_knowledge "
                "ON lifecycle_audit(knowledge_id, created_at)"
            )
            # Shadow hits are the governance layer's counterfactual ledger: how
            # often a retired record would still have been served.
            #
            # Stored as one aggregate row per retired record, not one row per
            # probe. Per-probe rows grow with *query volume*, which would make the
            # retirement bookkeeping grow faster than the library it governs — the
            # exact defect this layer exists to remove. An aggregate keeps the
            # table bounded by the number of retired records, which the retirement
            # policy already bounds.
            shadow_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(shadow_hits)"
                ).fetchall()
            }
            if shadow_columns and "id" in shadow_columns:
                # Per-probe layout from an earlier build. Dropping it loses only
                # probe counters; no record truth lives here, and the aggregate is
                # rebuilt by the next probe.
                connection.execute("DROP TABLE shadow_hits")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_hits (
                    knowledge_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    best_rank INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (knowledge_id, project_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_project "
                "ON shadow_hits(project_key, last_seen_at)"
            )
            self._ensure_column(connection, 'shadow_hits', 'query_hashes', "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, 'shadow_hits', 'decision_version', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, 'shadow_hits', 'content_hash', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, 'knowledge_records', 'last_restored_at', 'TEXT')
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS term_cooccurrence (
                    project_key TEXT NOT NULL,
                    term_a TEXT NOT NULL,
                    term_b TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (project_key, term_a, term_b)
                )
                """
            )
            # The pair is stored once in a canonical order, so a lookup for either
            # term needs an index on the other column: `term_a` is covered by the
            # primary key, and this one covers the reverse direction. Without it a
            # query-side lookup degrades to a full scan of the graph.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cooccurrence_b "
                "ON term_cooccurrence(project_key, term_b)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_probe_stats (
                    project_key TEXT PRIMARY KEY,
                    probe_count INTEGER NOT NULL DEFAULT 0,
                    missed_query_count INTEGER NOT NULL DEFAULT 0,
                    shadow_hit_count INTEGER NOT NULL DEFAULT 0,
                    resurrection_count INTEGER NOT NULL DEFAULT 0,
                    first_probe_at TEXT NOT NULL,
                    last_probe_at TEXT NOT NULL
                )
                """
            )
            if "source_session" not in legacy_record_columns:
                connection.execute(
                    """
                    UPDATE knowledge_records
                    SET status = 'candidate', updated_at = ?
                    WHERE status = 'active' AND verified_count = 0
                    """,
                    (self.clock(),),
                )
            versions.initialize(connection)

    def _subject_vocabulary(self, connection, *, project_key: str, statuses: str) -> tuple[str, ...]:
        """Return subject metadata with a write-versioned per-store cache."""
        revision_row = connection.execute(
            "SELECT revision FROM retrieval_revision WHERE singleton = 1"
        ).fetchone()
        revision = int(revision_row[0]) if revision_row else -1
        cache_key = (project_key, statuses)
        cached = self._subject_vocabulary_cache.get(cache_key)
        if cached and cached[0] == revision:
            return cached[1]
        rows = connection.execute(
            f"""SELECT subject_terms FROM knowledge_records
                WHERE status IN {statuses}
                  AND (scope = 'user' OR project_key = ?)
                LIMIT 5000""",
            (project_key,),
        ).fetchall()
        vocabulary = tuple(sorted({
            subject
            for item in rows
            for subject in normalize_subject_terms(item["subject_terms"])
        }))
        self._subject_vocabulary_cache[cache_key] = (revision, vocabulary)
        return vocabulary

    @staticmethod
    def _initialize_retrieval_revision(connection):
        # Random epochs prevent an uncommitted/rolled-back write from poisoning
        # a future transaction that happens to reach the same integer revision.
        connection.execute('''CREATE TABLE IF NOT EXISTS retrieval_revision (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL,
            epoch TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)''')
        connection.execute('''INSERT OR IGNORE INTO retrieval_revision(singleton,identity,epoch)
            VALUES (1,lower(hex(randomblob(16))),lower(hex(randomblob(16))))''')
        for event in ('INSERT', 'DELETE'):
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS mw_corpus_{event.lower()}
                AFTER {event} ON knowledge_records BEGIN
                UPDATE retrieval_revision SET epoch=lower(hex(randomblob(16))),revision=revision+1
                WHERE singleton=1; END''')
        # Recreate this trigger so databases created before subject_terms was
        # added also invalidate the cached subject vocabulary on metadata edits.
        connection.execute('DROP TRIGGER IF EXISTS mw_corpus_update')
        connection.execute('''CREATE TRIGGER mw_corpus_update
            AFTER UPDATE OF title,content,search_terms,subject_terms,status,scope,project_key ON knowledge_records
            WHEN OLD.title IS NOT NEW.title OR OLD.content IS NOT NEW.content
              OR OLD.search_terms IS NOT NEW.search_terms OR OLD.subject_terms IS NOT NEW.subject_terms
              OR OLD.status IS NOT NEW.status
              OR OLD.scope IS NOT NEW.scope OR OLD.project_key IS NOT NEW.project_key
            BEGIN UPDATE retrieval_revision SET epoch=lower(hex(randomblob(16))),revision=revision+1
            WHERE singleton=1; END''')

    @staticmethod
    def _invalidate_retrieval_statistics(connection):
        connection.execute('''UPDATE retrieval_revision
            SET epoch=lower(hex(randomblob(16))),revision=revision+1 WHERE singleton=1''')

    def rebuild_search_index(self, *, only_if_outdated=False):
        """Rebuild from authoritative records and invalidate stats atomically."""
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if only_if_outdated and connection.execute(
                'SELECT 1 FROM knowledge_migrations WHERE version=?', (SEARCH_INDEX_VERSION,)
            ).fetchone():
                return {'records': 0, 'changed': False, 'version': SEARCH_INDEX_VERSION}
            connection.execute('DELETE FROM knowledge_fts')
            records = connection.execute('SELECT id,title,content,search_terms FROM knowledge_records').fetchall()
            connection.executemany('INSERT INTO knowledge_fts(knowledge_id,title,content) VALUES (?,?,?)',
                [(r['id'],r['title'],indexed_content(r['title'],r['content'],r['search_terms'] or '')) for r in records])
            self._invalidate_retrieval_statistics(connection)
            connection.execute('INSERT OR IGNORE INTO knowledge_migrations(version) VALUES (?)',
                               (SEARCH_INDEX_VERSION,))
        return {'records': len(records), 'changed': True, 'version': SEARCH_INDEX_VERSION}

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def migrate_candidate_review_deadlines(self) -> int:
        """Shorten pending records carrying the old 72-hour default to 24 hours.

        Run at Runtime startup, not on recall. Keep custom deadlines, legacy
        NULLs, reviewed states and content timestamps untouched. The predicate
        makes retrying the upgrade harmless, including concurrent startups.
        """
        with self._connect() as connection:
            return connection.execute(
                """
                UPDATE knowledge_records
                SET candidate_expires_at = strftime(
                    '%Y-%m-%dT%H:%M:%S+00:00', created_at, ?
                )
                WHERE status = 'candidate'
                  AND julianday(candidate_expires_at) = julianday(created_at, '+3 days')
                """,
                (f"+{CANDIDATE_REVIEW_DAYS} days",),
            ).rowcount

    def publish(
        self,
        *,
        source_agent: str,
        project_key: str,
        title: str,
        content: str,
        knowledge_type: str,
        scope: str,
        evidence_summary: str,
        source_session: str | None = None,
        evidence_speaker: str | None = None,
        search_terms: str | None = None,
        subject_terms: list[str] | tuple[str, ...] | str | None = None,
    ) -> dict[str, Any]:
        self._validate_agent_id(source_agent)
        self._validate_project_key(project_key)
        title = self._required_text(title, "title", 160)
        content = self._required_text(content, "content", 8000)
        evidence_summary = self._required_text(
            evidence_summary, "evidence_summary", 1000
        )
        source_session = self._optional_text(source_session, "source_session", 160)
        evidence_speaker = self._optional_text(evidence_speaker, "evidence_speaker", 160)
        search_terms = self._optional_text(search_terms, "search_terms", 1000) or ""
        subject_terms = list(normalize_subject_terms(subject_terms))
        subject_terms_json = json.dumps(subject_terms, ensure_ascii=False, separators=(",", ":"))
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise ValueError(
                f"knowledge_type must be one of {sorted(KNOWLEDGE_TYPES)}"
            )
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}")

        digest_input = "\x1f".join(
            [title, content, knowledge_type, scope, project_key]
        )
        content_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        timestamp = self.clock()
        candidate_expires_at = (
            datetime.fromisoformat(timestamp) + timedelta(days=CANDIDATE_REVIEW_DAYS)
        ).isoformat(timespec="seconds")

        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                "SELECT * FROM knowledge_records WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if existing is not None:
                if (search_terms and search_terms != existing["search_terms"]) or (
                    subject_terms and normalize_subject_terms(existing["subject_terms"])
                    != tuple(subject_terms)
                ):
                    connection.execute(
                        "UPDATE knowledge_records SET search_terms = ?, subject_terms = ?, updated_at = ? WHERE id = ?",
                        (search_terms or existing["search_terms"], subject_terms_json, timestamp, existing["id"]),
                    )
                    connection.execute(
                        "DELETE FROM knowledge_fts WHERE knowledge_id = ?", (existing["id"],)
                    )
                    connection.execute(
                        "INSERT INTO knowledge_fts (knowledge_id, title, content) VALUES (?, ?, ?)",
                        (existing["id"], existing["title"], indexed_content(
                            existing["title"], existing["content"], search_terms or existing["search_terms"]
                        )),
                    )
                    existing = connection.execute(
                        "SELECT * FROM knowledge_records WHERE id = ?", (existing["id"],)
                    ).fetchone()
                connection.execute(
                    """
                    INSERT INTO knowledge_evidence (
                        id, knowledge_id, agent_id, outcome, summary,
                        evidence_kind, evidence_ref, status_before, status_after,
                        created_at
                    ) VALUES (?, ?, ?, 'confirmed_duplicate', ?, 'duplicate', '', ?, ?, ?)
                    """,
                    (
                        f"ev_{uuid.uuid4().hex[:16]}",
                        existing["id"],
                        source_agent,
                        evidence_summary,
                        existing["status"],
                        existing["status"],
                        timestamp,
                    ),
                )
                return {
                    "status": "existing",
                    "deduplicated": True,
                    "knowledge": self._public_record(existing, source_agent),
                }

            knowledge_id = f"kn_{uuid.uuid4().hex[:16]}"
            evidence_id = f"ev_{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO knowledge_records (
                    id, title, content, knowledge_type, scope, project_key,
                    source_agent, status, source_session, evidence_speaker, subject_terms, search_terms, content_hash,
                    candidate_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    knowledge_id,
                    title,
                    content,
                    knowledge_type,
                    scope,
                    project_key,
                    source_agent,
                    source_session,
                    evidence_speaker,
                    subject_terms_json,
                    search_terms,
                    content_hash,
                    candidate_expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_evidence (
                    id, knowledge_id, agent_id, outcome, summary,
                    evidence_kind, evidence_ref, status_before, status_after,
                    created_at
                ) VALUES (?, ?, ?, 'proposed', ?, 'observation', '', NULL, 'candidate', ?)
                """,
                (
                    evidence_id,
                    knowledge_id,
                    source_agent,
                    evidence_summary,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_fts (knowledge_id, title, content) VALUES (?, ?, ?)",
                (knowledge_id, title, indexed_content(title, content, search_terms)),
            )
            # Record the vocabulary bridges this record creates. Done here, on the
            # write path, because the pairing depends on what the corpus already
            # contained and the query path must never do this work: it is one
            # indexed read per publication, against a second FTS pass per recall.
            self._record_term_bridges(
                connection,
                project_key=project_key,
                title=title,
                search_terms=search_terms,
                timestamp=timestamp,
            )
            record = connection.execute(
                "SELECT * FROM knowledge_records WHERE id = ?", (knowledge_id,)
            ).fetchone()
            versions.index_record(connection, record)
            record = connection.execute('SELECT * FROM knowledge_records WHERE id=?',(knowledge_id,)).fetchone()

        return {
            "status": "created",
            "deduplicated": False,
            "next_action": (
                "Validate this candidate with an objective check, then call "
                "knowledge_feedback with outcome='verified', evidence_kind, and evidence_ref."
            ),
            "knowledge": self._public_record(record, source_agent),
        }

    def search(
        self,
        *,
        requester_agent: str,
        project_key: str,
        query: str,
        limit: int,
        include_retired: bool = False,
        expand_siblings: bool = True,
        retrieval_policy: RetrievalPolicy | None = None,
    ) -> dict[str, Any]:
        """Run candidate generation, expansion, arbitration and truncation.

        Default policy preserves the legacy composition. expand_siblings=False
        disables ALL expansion, including bridge and anchor, for governance
        counterfactuals. include_retired includes archived (never quarantined)
        records without lifecycle demotion. Policy stage names are an enablement
        set; execution order is always bridge -> sibling -> anchor.
        """
        self._validate_agent_id(requester_agent)
        self._validate_project_key(project_key)
        query = self._required_text(query, "query", 500)
        original_query = query
        intent = query_intent(query, project_key=project_key)
        query = intent.focus
        history_lookup = intent.historical and not include_retired
        if history_lookup:
            include_retired = True
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        policy = retrieval_policy or RetrievalPolicy(slack=BRIDGE_SLACK)
        statuses = "('active', 'stale', 'archived')" if include_retired else "('active', 'stale')"
        reports = []
        with self._connect() as connection:
            # Version, statistics and rows belong to the same SQLite snapshot.
            connection.execute("BEGIN")
            statistics = StatisticsSnapshot(connection, self.database_path, bounded_document_frequencies)
            started = perf_counter()
            rows, fallback, has_terms = self._generate_candidates(
                connection, query=query, project_key=project_key, limit=limit,
                include_retired=include_retired, statuses=statuses, statistics=statistics)
            reports.append({"stage": "candidate_generation", "added_count": len(rows),
                            "fallback_count": len(fallback),
                            "elapsed_ms": round((perf_counter() - started) * 1000, 3)})
            # Rejected direct hits must not seed sibling expansion. Apply the
            # same policy again after expansion so every route is accountable.
            from agent_knowledge_bridge.retrieval_pipeline import Candidate
            seeds, fallback, seed_omitted = filter_candidates(tuple(Candidate(r) for r in rows), fallback, intent)
            # Build vocabulary from all retrievable metadata. Looking only at
            # the current page cannot prove that a returned row belongs to a
            # different subject when the requested subject is absent there.
            subject_vocabulary = self._subject_vocabulary(
                connection, project_key=project_key, statuses=statuses
            )
            seeds, evidence_seed_omitted = filter_retrieval_candidates(
                seeds, query, subject_vocabulary=subject_vocabulary
            )
            seed_omitted.extend(evidence_seed_omitted)
            rows = [hit.row for hit in seeds]
            context = StageContext(self, connection, query, project_key, statuses, limit,
                                   include_retired, policy, statistics)
            # General/external lookups should not fan out from a coincidental
            # topic word into siblings and anchors. A memory may still answer
            # an explicit technical question, but expansion is reserved for a
            # query that has a concrete task or subject signal.
            candidates, expansion_reports = expand(
                context, rows,
                enabled=(expand_siblings and not intent.general_explanation
                         and has_terms and (bool(rows) or not seed_omitted)),
            )
            reports.extend(expansion_reports)
            started = perf_counter()
            candidates, fallback, decision_omitted = filter_candidates(candidates, fallback, intent)
            candidates, evidence_omitted = filter_retrieval_candidates(
                candidates, query, subject_vocabulary=subject_vocabulary
            )
            decision_omitted.extend(evidence_omitted)
            candidates, answerability_omitted = apply_answerability_gate(
                candidates, query, subject_vocabulary=subject_vocabulary
            )
            decision_omitted.extend(answerability_omitted)
            reports.append({
                "stage": "answerability",
                "input_count": len(candidates) + len(answerability_omitted),
                "output_count": len(candidates),
                "omitted_count": len(answerability_omitted),
                "reason": (
                    answerability_omitted[0]["reason"]
                    if answerability_omitted else "supported_or_unresolved"
                ),
                "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            })
            if answerability_omitted and not candidates:
                # A rejected answerability page must not be resurrected by the
                # LIKE fallback during arbitration.
                fallback = []
            ranked = arbitrate(candidates, fallback, limit, connection=connection if expand_siblings else None)
            reports.append({"stage": "arbitration", "input_count": len(candidates),
                            "experience_rank_changes": ranked.adaptive_changes,
                            "elapsed_ms": round((perf_counter() - started) * 1000, 3)})
            started = perf_counter()
            selected, omitted = truncate(ranked, limit=limit, slack=policy.slack if expand_siblings else 0)
            # LIKE fallback and row truncation must satisfy the same gates. A
            # page that passed before truncation may have lost its evidence.
            selected, final_evidence_omitted = filter_retrieval_candidates(
                selected, query, subject_vocabulary=subject_vocabulary)
            selected, final_answer_omitted = apply_answerability_gate(
                selected, query, subject_vocabulary=subject_vocabulary)
            omitted += final_evidence_omitted + final_answer_omitted
            omitted = seed_omitted + decision_omitted + omitted
            reports.append({"stage": "truncation", "output_count": len(selected),
                            "elapsed_ms": round((perf_counter() - started) * 1000, 3)})
            results = []
            for hit in selected:
                record = {**self._public_record(hit.row, requester_agent, include_content=True),
                          "retrieval_score": hit.row["rank"], "retrieval_method": "fts5-enriched",
                          "origin": hit.origin,
                          "provenance": ranked.provenance.get(hit.id, [hit.provenance()])}
                if hit.parent_id:
                    record.update(related_to=hit.parent_id, related_title=hit.parent_title,
                                  related_origin=hit.parent_origin)
                results.append(record)
        return {"query": original_query, "requester_agent": requester_agent, "project_key": project_key,
                "count": len(results), "results": results,
                "retrieval_diagnostics": {
                    "policy": "legacy-composition-v1", "stages": reports,
                    "decision_policy": POLICY_VERSION, "query_focus_changed": query != original_query,
                    "enabled_stages": list(policy.stages) if expand_siblings and has_terms else [],
                    "sibling_seed_origins": list(policy.sibling_seed_origins),
            "max_depth": policy.max_depth, "statistics": statistics.metrics(), "omitted": omitted}}

    def _generate_candidates(self, connection, *, query, project_key, limit,
                             include_retired, statuses, statistics):
        """Generate a relevance pool; pin rare identifiers for expansion seeds.

        This initial order intentionally remains compatible with legacy topic
        inference. Cross-route placement and final budgets live in the pipeline.
        The LIKE fallback is used only if the full pipeline produces no result.
        """
        tokens = retrieval_tokens(query)
        match_expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        pool = max(limit * 8, 24)
        rows = []
        if match_expression:
            try:
                if include_retired:
                    rows = connection.execute(
                        f"""
                        SELECT r.*, bm25(knowledge_fts) AS rank
                        FROM knowledge_fts
                        JOIN knowledge_records r ON r.id = knowledge_fts.knowledge_id
                        WHERE knowledge_fts MATCH ?
                          AND r.status IN {statuses}
                          AND (r.scope = 'user' OR r.project_key = ?)
                        ORDER BY rank ASC, r.verified_count DESC, r.updated_at DESC
                        LIMIT ?
                        """,
                        (match_expression, project_key, pool),
                    ).fetchall()
                else:
                    # Take a relevance pool from each retrievable lifecycle state.
                    # A global status-first LIMIT can discard an exact stale match
                    # before the discriminative re-ranker ever sees it.
                    per_status: list[sqlite3.Row] = []
                    for status in ("active", "stale"):
                        per_status.extend(
                            connection.execute(
                                """
                                SELECT r.*, bm25(knowledge_fts) AS rank
                                FROM knowledge_fts
                                JOIN knowledge_records r
                                  ON r.id = knowledge_fts.knowledge_id
                                WHERE knowledge_fts MATCH ?
                                  AND r.status = ?
                                  AND (r.scope = 'user' OR r.project_key = ?)
                                ORDER BY rank ASC, r.verified_count DESC,
                                         r.updated_at DESC
                                LIMIT ?
                                """,
                                (match_expression, status, project_key, pool),
                            ).fetchall()
                        )
                    rows = sorted(
                        per_status,
                        key=lambda row: (
                            0 if row["status"] == "active" else 1,
                            float(row["rank"]),
                            -int(row["verified_count"]),
                        ),
                    )
            except sqlite3.OperationalError:
                rows = []
            rows = filter_rows(rows, query)
            rows = self._pin_discriminative(
                connection,
                rows,
                query=query,
                limit=limit,
                status_aware=not include_retired,
                statistics=statistics,
            )

        fallback = []
        if not rows:
            status_rank = "" if include_retired else (
                "CASE WHEN r.status = 'archived' THEN 2 WHEN r.status = 'stale' THEN 1 ELSE 0 END,")
            like_query = f"%{query}%"
            fallback = connection.execute(
                f"""SELECT r.*, 0.0 AS rank FROM knowledge_records r
                    WHERE r.status IN {statuses}
                      AND (r.scope = 'user' OR r.project_key = ?)
                      AND (r.title LIKE ? OR r.content LIKE ?)
                    ORDER BY {status_rank} r.verified_count DESC, r.updated_at DESC LIMIT ?""",
                (project_key, like_query, like_query, limit)).fetchall()
        return rows, filter_rows(fallback, query), bool(match_expression)

    def mark_hits(self, knowledge_ids: list[str]) -> int:
        """Record that these records were injected into a caller's context.

        A hit is weaker than adoption: it says the record was still being served,
        not that anyone used it. It is still the only signal that distinguishes a
        record that keeps earning its retrieval slot from one that has never been
        served since it was written, which is what retirement decisions rest on.
        Returns the number of records updated.
        """
        ids = [value for value in dict.fromkeys(knowledge_ids) if value]
        if not ids:
            return 0
        timestamp = self.clock()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE knowledge_records
                SET hit_count = hit_count + 1, last_hit_at = ?
                WHERE id IN ({placeholders})
                """,
                (timestamp, *ids),
            )
            return cursor.rowcount

    def transit(
        self,
        knowledge_id: str,
        *,
        to_status: str,
        reason: str,
        actor: str,
        expected_status: str | None = None,
        restoration_digest: str | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Move one record to another lifecycle status and audit the transition.

        Retirement is expressed as a status change, never a delete, so a record
        that turns out to be needed again can be restored and its history is intact.
        """
        if to_status not in LIFECYCLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(LIFECYCLE_STATUSES)}")
        if expected_status is not None and expected_status not in LIFECYCLE_STATUSES:
            raise ValueError(
                f"expected_status must be one of {sorted(LIFECYCLE_STATUSES)}"
            )
        self._validate_agent_id(actor)
        reason = self._required_text(reason, "reason", 500)
        # Reuse emission can share the caller's writer transaction, so a failed
        # trace write rolls back restoration too. Public callers still get an
        # independent transaction. Never commit the caller-owned connection.
        if _connection is None:
            with self._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.transit(
                    knowledge_id, to_status=to_status, reason=reason, actor=actor,
                    expected_status=expected_status, restoration_digest=restoration_digest,
                    _connection=connection,
                )
        if not _connection.in_transaction:
            raise ValueError('lifecycle transition requires a writer transaction')
        return self._transit_locked(
            _connection, knowledge_id, to_status=to_status, reason=reason, actor=actor,
            expected_status=expected_status, restoration_digest=restoration_digest,
        )

    def _transit_locked(self, connection, knowledge_id, *, to_status, reason,
                        actor, expected_status, restoration_digest):
        timestamp = self.clock()
        # Keep all guards and bounded audit writes on the same connection.
        record = connection.execute(
            "SELECT * FROM knowledge_records WHERE id = ?", (knowledge_id,)
        ).fetchone()
        if record is None:
            raise ValueError("knowledge record not found")
        if to_status in {'active','stale'} and versions.blocked(record):
            return {'changed': False, 'conflict': True, 'reason': versions.blocked(record)}
        if restoration_digest is not None and (record_digest(record) != restoration_digest or obsolete(record)):
            return {'changed': False, 'conflict': True, 'reason': 'restoration_evidence_changed'}
        from_status = record["status"]
        if expected_status is not None and from_status != expected_status:
            return {
                "transition": {"from": from_status, "to": to_status},
                "changed": False,
                "conflict": True,
            }
        if from_status == to_status:
            return {"transition": {"from": from_status, "to": to_status},
                    "changed": False}
        cursor = connection.execute(
            "UPDATE knowledge_records SET status = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (to_status, timestamp, knowledge_id, from_status),
        )
        if cursor.rowcount != 1:
            return {
                "transition": {"from": from_status, "to": to_status},
                "changed": False,
                "conflict": True,
            }
        if restoration_digest is not None:
            connection.execute('UPDATE knowledge_records SET last_restored_at=? WHERE id=?',
                               (timestamp, knowledge_id))
        versions.refresh(connection, record)
        connection.execute(
            """
            INSERT INTO lifecycle_audit
                (id, knowledge_id, from_status, to_status, reason, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"lc_{uuid.uuid4().hex[:16]}", knowledge_id, from_status, to_status,
             reason, actor, timestamp),
        )
        # Keep the recent history and drop the tail. A record that oscillates
        # between active and retired would otherwise grow the audit table
        # without bound, which is the same unbounded-growth defect the
        # lifecycle layer exists to fix. The records themselves are never
        # touched here; only their older bookkeeping rows.
        connection.execute(
            """
            DELETE FROM lifecycle_audit
            WHERE knowledge_id = ?
              AND id NOT IN (
                  SELECT id FROM lifecycle_audit WHERE knowledge_id = ?
                  ORDER BY created_at DESC, rowid DESC LIMIT ?
              )
            """,
            (knowledge_id, knowledge_id, AUDIT_ROWS_PER_RECORD),
        )
        return {"transition": {"from": from_status, "to": to_status}, "changed": True}

    def lifecycle_audit(self, knowledge_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if knowledge_id:
                rows = connection.execute(
                    "SELECT * FROM lifecycle_audit WHERE knowledge_id = ? "
                    "ORDER BY created_at ASC", (knowledge_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM lifecycle_audit ORDER BY created_at ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def _pin_discriminative(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        query: str,
        limit: int,
        status_aware: bool = True,
        statistics=None,
    ) -> list[sqlite3.Row]:
        """Lift records that mention the query's rare composite identifier.

        BM25 length normalisation rewards short records that match many shared
        query terms, so in a homogeneous corpus a record answering a different
        entity can outrank the one the caller asked about. The identifier that
        occurs in only a few records is the strongest relevance signal available,
        so it becomes the primary sort key; BM25 order is preserved inside each
        partition and the original ordering is returned unchanged when no such
        identifier exists.

        Records sharing that identifier are, by construction, about the same
        entity — which is exactly where contradictions live. Inside the pinned
        partition the number of confirming verifications outranks BM25, so a
        well-evidenced record is not buried under a same-entity record that merely
        matches the wording more closely. Without this a single wrong record with
        slightly better term overlap wins the top slot and the caller reads it
        first. Ties keep the incoming BM25 order because the sort is stable.

        Lifecycle rank is applied inside the pin as well, because re-sorting a
        page that was already ordered by status would otherwise undo the demotion:
        a `stale` record with a better BM25 rank would climb back over an `active`
        one and retirement would have no effect. A shadow probe passes
        `status_aware=False`, since measuring what retirement cost requires
        ranking as though status had never changed.
        """
        if len(rows) < 2:
            return rows
        scored: list[tuple[int, int, int, str]] = []
        terms = DISCRIMINATIVE_TOKEN.findall(query)
        frequencies = (statistics.frequencies(terms, ceiling=limit) if statistics else
                       bounded_document_frequencies(connection, terms, ceiling=limit))
        for token, frequency in frequencies.items():
            if 0 < frequency <= limit:
                # Rarest first; then instance-style names (-) over field names (_);
                # then the longest, which carries the most information.
                scored.append(
                    (frequency, 0 if "-" in token else 1, -len(token), token)
                )
        if not scored:
            return rows
        anchor = min(scored)[3]
        try:
            pinned = connection.execute(
                "SELECT knowledge_id FROM knowledge_fts WHERE knowledge_fts MATCH ?",
                (f'"{anchor}"',),
            ).fetchall()
        except sqlite3.OperationalError:
            return rows
        pinned_ids = {row[0] for row in pinned}
        # Preserve the incoming relevance order. The FTS membership query above
        # has no ORDER BY and therefore cannot be used as a BM25 rank source.
        rank_of = {
            row["id"]: index
            for index, row in enumerate(rows)
            if row["id"] in pinned_ids
        }
        if not rank_of:
            return rows

        penalties = {"active": 0, "stale": 1, "archived": 2}

        def sort_key(row: sqlite3.Row) -> tuple[int, int, int, int]:
            if row["id"] not in rank_of:
                return (1, 0, 0, 0)
            penalty = penalties.get(row["status"], 0) if status_aware else 0
            return (0, penalty, -int(row["verified_count"]), rank_of[row["id"]])

        return sorted(rows, key=sort_key)

    def _term_bridge_pairs(
        self,
        connection: sqlite3.Connection,
        *,
        project_key: str,
        title: str,
        search_terms: str,
    ) -> set[tuple[str, str]]:
        """Compute the term associations one record contributes, without storing.

        Separated from persistence because the same computation serves both the
        live write path and a full rebuild, and their storage needs differ: a
        publication adds one record's contribution to a graph that already
        contains the rest, while a rebuild recomputes every record's from
        nothing. Keeping the count out of this function is what stops the two
        from disagreeing.
        """
        terms = _bridge_terms(title, search_terms)
        if not terms:
            return set()
        # Which terms survive the cap must be decided by how much they say, not
        # by an accident of encoding. Sorting by codepoint put every Latin term
        # ahead of every Chinese one -- `artifact` ranked below `第三方渠道` -- so
        # the cap kept the ASCII half of each record and 58% of the corpus's
        # Chinese vocabulary never entered the graph at all. Frequency is the
        # real signal: a term that a handful of records share is what
        # distinguishes a subject, and a term the whole project repeats has no
        # association to contribute.
        neighbour_rows = connection.execute(
            """
            SELECT title, search_terms FROM knowledge_records
            WHERE project_key = ? AND title != ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (project_key, title, SIBLING_CANDIDATE_CAP),
        ).fetchall()
        frequency: dict[str, int] = {term: 0 for term in terms}
        for neighbour_row in neighbour_rows:
            for term in _bridge_terms(
                neighbour_row["title"], neighbour_row["search_terms"] or ""
            ):
                if term in frequency:
                    frequency[term] += 1
        ordered = sorted(terms, key=lambda term: (frequency[term], term))

        pairs: set[tuple[str, str]] = set()
        for index, term in enumerate(ordered[:MAX_EDGES_PER_TERM]):
            for other in ordered[index + 1:MAX_EDGES_PER_TERM]:
                pairs.add((min(term, other), max(term, other)))

        for neighbour_row in neighbour_rows:
            shared = terms & _bridge_terms(
                neighbour_row["title"], neighbour_row["search_terms"] or ""
            )
            if not shared:
                continue
            # Pair across, never within: the shared term is what the two records
            # already agree on, so an edge between them states nothing new. What
            # is worth recording is which of the new record's terms travelled
            # with it, because that is the alternation a later query can exploit.
            for term in sorted(shared, key=lambda t: (frequency[t], t))[:MAX_EDGES_PER_TERM]:
                for other in ordered[:MAX_EDGES_PER_TERM]:
                    if other == term or other in shared:
                        continue
                    pairs.add((min(term, other), max(term, other)))
        return pairs

    def _record_term_bridges(
        self,
        connection: sqlite3.Connection,
        *,
        project_key: str,
        title: str,
        search_terms: str,
        timestamp: str,
    ) -> None:
        """Note which terms this record associates, for the query-side bridge.

        Every record already carries a project and a title the author wrote, and
        the records an Agent produced in one sitting are a usable statement that
        they belong together. Two signals come out of that and both are stored
        here, once, on the write path:

        * within-record -- the terms sharing one title/terms line are an
          assertion that they name the same subject;
        * cross-record -- a term shared with a recent neighbour, paired against
          the neighbour's *other* terms, is the association that lets a query
          for one phrasing reach a record written in another.

        The second is the one that matters. Storing it at publication is what
        keeps the query path to a single indexed read: the alternative is
        recomputing the corpus's term neighbourhoods on every recall, which is a
        full pass over the store per query and does not fit the latency budget.
        """
        pairs = self._term_bridge_pairs(
            connection,
            project_key=project_key,
            title=title,
            search_terms=search_terms,
        )
        if not pairs:
            return
        # A pair is written in one canonical order and its reverse is derived at
        # read time, so the table holds each association once.
        #
        # ``seen_count`` is the number of records that assert the pairing -- the
        # only thing separating a real variant pairing from one author's phrasing,
        # and the value the query side thresholds on. It is incremented, not
        # assigned a constant. Assigning one made the write path and the rebuild
        # disagree on every edge: the rebuild counted the records and served the
        # association, while a corpus built by publishing alone left every edge at
        # one, below ``MIN_COOCCURRENCE``, so the bridge never fired at all.
        #
        # Incrementing counts the same thing the rebuild counts -- records, not
        # runs -- because this executes exactly once per publication, inside the
        # transaction that inserts the record. Replaying the backfill is still
        # safe: that path recomputes every count from the corpus, so it both
        # repairs any drift and cannot be double-counted by a second run.
        connection.executemany(
            """
            INSERT INTO term_cooccurrence
                (project_key, term_a, term_b, seen_count, last_seen_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(project_key, term_a, term_b) DO UPDATE SET
                seen_count = seen_count + 1,
                last_seen_at = MAX(last_seen_at, excluded.last_seen_at)
            """,
            [
                (project_key, term_a, term_b, timestamp)
                for term_a, term_b in sorted(pairs)
            ],
        )

    def rebuild_term_bridges(self, *, project_key: str | None = None) -> dict[str, int]:
        """Recompute the co-occurrence graph from the records themselves.

        The graph is derived state: it is a pure function of the titles and
        search terms already stored, and nothing here is user-authored. That
        makes a rebuild always available rather than a migration -- a corpus
        written before the graph existed, or one whose term selection has since
        been corrected, can be brought up to date without re-publishing
        anything.

        Rebuilt into a temporary table and swapped in one transaction, so a
        failure part-way through leaves the previous graph serving reads rather
        than an empty one. Idempotent by construction: the counts are recomputed
        from the corpus, not accumulated into it, so running this twice produces
        the same table.
        """
        with self._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS term_cooccurrence_rebuild")
            connection.execute(
                """
                CREATE TABLE term_cooccurrence_rebuild (
                    project_key TEXT NOT NULL,
                    term_a TEXT NOT NULL,
                    term_b TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (project_key, term_a, term_b)
                )
                """
            )
            records = connection.execute(
                """
                SELECT project_key, title, search_terms, created_at
                FROM knowledge_records
                WHERE (? IS NULL OR project_key = ?)
                ORDER BY created_at ASC, rowid ASC
                """,
                (project_key, project_key),
            ).fetchall()
            # Counted per record rather than accumulated: each record that
            # asserts a pairing increments it once, which is what makes the
            # result independent of how many times this has run.
            counts: dict[tuple[str, str, str], int] = {}
            latest: dict[tuple[str, str, str], str] = {}
            for record in records:
                for term_a, term_b in self._term_bridge_pairs(
                    connection,
                    project_key=record["project_key"],
                    title=record["title"],
                    search_terms=record["search_terms"] or "",
                ):
                    key = (record["project_key"], term_a, term_b)
                    counts[key] = counts.get(key, 0) + 1
                    stamp = record["created_at"]
                    if stamp > latest.get(key, ""):
                        latest[key] = stamp
            connection.executemany(
                """
                INSERT INTO term_cooccurrence_rebuild
                    (project_key, term_a, term_b, seen_count, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_key, term_a, term_b) DO UPDATE SET
                    seen_count = seen_count + excluded.seen_count,
                    last_seen_at = MAX(last_seen_at, excluded.last_seen_at)
                """,
                [
                    (key[0], key[1], key[2], count, latest[key])
                    for key, count in counts.items()
                ],
            )
            connection.execute("DROP TABLE IF EXISTS term_cooccurrence")
            connection.execute(
                "ALTER TABLE term_cooccurrence_rebuild RENAME TO term_cooccurrence"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cooccurrence_b "
                "ON term_cooccurrence(project_key, term_b)"
            )
            # The swap above replaces the table wholesale, so whatever statistics
            # the planner held describe a table that no longer exists. Left stale,
            # a lookup on this graph is planned against the old cardinality and
            # degrades by roughly an order of magnitude -- measured 9.95ms against
            # 1.44ms once ``ANALYZE`` has run. Restatisticising here is what keeps
            # the rebuild from silently costing the hot path.
            connection.execute("ANALYZE term_cooccurrence")
            total = connection.execute(
                "SELECT COUNT(*) FROM term_cooccurrence"
            ).fetchone()[0]
            strong = connection.execute(
                "SELECT COUNT(*) FROM term_cooccurrence WHERE seen_count >= ?",
                (MIN_COOCCURRENCE,),
            ).fetchone()[0]
        return {"records": len(records), "edges": total, "strong_edges": strong}

    def _expand_vocabulary(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        project_key: str,
        statuses: str,
        seen: set[str],
    ) -> list[sqlite3.Row]:
        """Re-run the query with terms the corpus associates with its own.

        This is the third tier of cross-Agent recall failure and the only one
        that needs anything resembling an algorithm. The first two are handled
        elsewhere: sibling expansion already repairs the case where the query
        matched one record of a cluster, and a shared term is what it keys on.
        What is left is the query that shares no term at all, where an Agent
        asked in its own vocabulary for something written in another's.

        The bridge is looked up, never computed. A term is only swapped for its
        recorded neighbours once the association has been seen
        ``MIN_COOCCURRENCE`` times, and the caller only pays for the second FTS
        pass when the first one came back thin.
        """
        terms = sorted(_bridge_terms(query, ""))
        if not terms:
            return []
        placeholders = ", ".join("?" for _ in terms)
        # GROUP BY is load-bearing, not decoration. Without it these aggregates
        # over zero matching rows still return one row -- SQL defines MAX() over
        # an empty set as NULL -- so a term whose every edge sat below the
        # threshold produced a "candidate" of NULL, was appended to the match
        # expression as a bogus token, and triggered the second FTS pass on a
        # query the corpus never associated with anything. Grouping makes the
        # aggregate per-term, so a term with no qualifying edge returns no row
        # and the caller correctly sees an empty result.
        scored = connection.execute(
            f"""
            SELECT term_b AS term, MAX(seen_count) AS strength
            FROM term_cooccurrence
            WHERE project_key = ? AND term_a IN ({placeholders})
              AND seen_count >= ?
            GROUP BY term_b
            UNION ALL
            SELECT term_a AS term, MAX(seen_count) AS strength
            FROM term_cooccurrence
            WHERE project_key = ? AND term_b IN ({placeholders})
              AND seen_count >= ?
            GROUP BY term_a
            ORDER BY strength DESC, term ASC LIMIT ?
            """,
            (
                project_key, *terms, MIN_COOCCURRENCE,
                project_key, *terms, MIN_COOCCURRENCE,
                MAX_QUERY_EXTENSIONS,
            ),
        ).fetchall()
        if not scored:
            return []
        # The extension terms are the query's own vocabulary restated. They are
        # appended rather than substituted: the original terms are what the
        # caller actually asked with, and dropping them to chase a variant would
        # trade a certain match for a speculative one.
        extended = list(retrieval_tokens(query))
        known = {token.lower() for token in extended}
        for row in scored:
            term = str(row["term"])
            if term not in known:
                extended.append(term)
                known.add(term)
        if len(extended) == len(retrieval_tokens(query)):
            return []
        match_expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in extended[:64]
        )
        try:
            found = connection.execute(
                f"""
                SELECT r.*, bm25(knowledge_fts) AS rank
                FROM knowledge_fts
                JOIN knowledge_records r ON r.id = knowledge_fts.knowledge_id
                WHERE knowledge_fts MATCH ?
                  AND r.status IN {statuses}
                  AND (r.scope = 'user' OR r.project_key = ?)
                ORDER BY rank ASC, r.verified_count DESC, r.updated_at DESC
                LIMIT ?
                """,
                (match_expression, project_key, EXPANSION_POOL),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return filter_rows([row for row in found if row["id"] not in seen], query)

    def _expand_anchored(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        project_key: str,
        statuses: str,
        room: int,
        seen: set[str],
        statistics=None,
    ) -> list[sqlite3.Row]:
        """Append records that name the same rare term as the query did.

        The discriminative pin generalised past composite identifiers. Where the
        pin reorders records the direct pass already returned, this reaches the
        ones it never returned: a query naming ``第三方`` may match a dozen
        records that repeat its common words and rank the four records that
        actually carry the subject below the cut. Those four are the answer, and
        no amount of ranking inside the returned page can recover them.

        The anchor is the rarest term the query names -- see ``anchor_terms`` for
        why rarity and not punctuation is the property that identifies a subject.
        Any term occurring in more records than ``MAX_ANCHOR_DF`` is rejected, so
        a query made only of common words expands to nothing rather than dragging
        in the whole project. Frequency selection uses bounded, batched phrase
        counts and a bounded fetch. It runs whenever the
        expansion budget has room, including a full direct page with slack.
        """
        if room <= 0:
            return []
        anchors = anchor_terms(connection, retrieval_tokens(query), statistics=statistics)
        if not anchors:
            return []
        match_expression = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in anchors[:8]
        )
        try:
            found = connection.execute(
                f"""
                SELECT r.*, bm25(knowledge_fts) AS rank
                FROM knowledge_fts
                JOIN knowledge_records r ON r.id = knowledge_fts.knowledge_id
                WHERE knowledge_fts MATCH ?
                  AND r.status IN {statuses}
                  AND (r.scope = 'user' OR r.project_key = ?)
                ORDER BY rank ASC, r.verified_count DESC, r.updated_at DESC
                LIMIT ?
                """,
                (match_expression, project_key, room + len(seen)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return filter_rows([row for row in found if row["id"] not in seen], query)[:room]

    def _sibling_additions(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        statuses: str,
        project_key: str,
    ) -> tuple[list[sqlite3.Row], dict[str, str]]:
        """Append records about the same subject as the records just retrieved.

        Retrieval matches one record at a time, but knowledge is stored in
        clusters: a project accumulates dozens of records that only make sense
        together, and a query reaches whichever of them happens to share wording
        with it. The caller then holds a fact with no idea which work it belongs
        to -- it learns the phrase "third-party relay" and not which paper the
        phrase constrains.

        Subject is inferred from the records that already ranked: the anchor set
        contributes its own whole terms, and a candidate that shares several of
        them is about the same thing. Terms are weighted by how rare they are
        inside the candidate pool, because in a single-project corpus the common
        words ("paper", "论文") are shared by most records and would otherwise
        let any of them qualify on their own.

        Return additions and their parent mapping only. The pipeline arbitrator
        owns placement; this helper never mutates or truncates the input pool.
        """
        if not rows:
            return [], {}
        topic: set[str] = set()
        for row in rows[:3]:
            topic |= _whole_terms(f"{row['title']} {row['search_terms'] or ''}")
        if len(topic) < MIN_SIBLING_OVERLAP:
            return [], {}

        known = {row["id"] for row in rows}
        placeholders = ", ".join("?" for _ in known)
        candidates = connection.execute(
            f"""
            SELECT r.*, 0.0 AS rank
            FROM knowledge_records r
            WHERE r.status IN {statuses}
              AND (r.scope = 'user' OR r.project_key = ?)
              AND r.id NOT IN ({placeholders})
            ORDER BY r.verified_count DESC, r.updated_at DESC
            LIMIT ?
            """,
            (project_key, *known, SIBLING_CANDIDATE_CAP),
        ).fetchall()

        # Document frequency inside the pool, so a term carried by nearly every
        # record contributes almost nothing and a rare one carries the match.
        frequency: dict[str, int] = {}
        candidate_terms: list[tuple[sqlite3.Row, set[str]]] = []
        for candidate in candidates:
            terms = _whole_terms(
                f"{candidate['title']} {candidate['search_terms'] or ''}"
            )
            candidate_terms.append((candidate, terms))
            for term in terms:
                frequency[term] = frequency.get(term, 0) + 1

        scored: list[tuple[float, int, sqlite3.Row]] = []
        for candidate, terms in candidate_terms:
            shared = terms & topic
            if len(shared) < MIN_SIBLING_OVERLAP:
                continue
            weight = sum(1.0 / frequency[term] for term in shared if term in frequency)
            scored.append((weight, len(shared), candidate))
        if not scored:
            return [], {}
        # Rarer shared terms first; the count only breaks ties, so a record
        # sharing one distinctive term outranks one sharing three common ones.
        scored.sort(key=lambda item: (-item[0], -item[1], -int(item[2]["verified_count"])))
        siblings = [candidate for _weight, _count, candidate in scored[:MAX_SIBLINGS]]

        # Each sibling travels with the id of the record it was inferred from. A
        # caller that reads `origin: sibling` alone still does not know which work
        # it belongs to -- it would learn the phrase "第三方渠道" and not which
        # paper constrains it, which is the same failure one hop later. Shipping
        # the anchor makes the inference legible instead of merely flagged.
        anchors: dict[str, str] = {}
        for candidate in siblings:
            terms = next(t for row_, t in candidate_terms if row_["id"] == candidate["id"])
            best = max(
                rows[:3],
                key=lambda row_: len(terms & _whole_terms(
                    f"{row_['title']} {row_['search_terms'] or ''}")),
            )
            anchors[candidate["id"]] = best["id"]
        # The ids travel back with the rows so the caller can tell an inferred
        # neighbour from a record the query matched, which governance needs in
        # order to keep its recall accounting honest.
        return siblings, anchors

    def review_queue(
        self,
        *,
        requester_agent: str,
        project_key: str,
        status: str = "candidate",
        limit: int = 20,
    ) -> dict[str, Any]:
        self._validate_agent_id(requester_agent)
        self._validate_project_key(project_key)
        if status != "all" and status not in REVIEW_STATUSES:
            raise ValueError(
                f"status must be 'all' or one of {sorted(REVIEW_STATUSES)}"
            )
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        statuses = tuple(sorted(REVIEW_STATUSES)) if status == "all" else (status,)
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM knowledge_records
                WHERE status IN ({placeholders})
                  AND (scope = 'user' OR project_key = ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (*statuses, project_key, limit),
            ).fetchall()

        return {
            "requester_agent": requester_agent,
            "project_key": project_key,
            "status_filter": status,
            "count": len(rows),
            "results": [
                self._public_record(row, requester_agent, include_content=True)
                for row in rows
            ],
        }

    def list_records(
        self,
        *,
        requester_agent: str,
        project_key: str,
        status: str = "all",
        limit: int = 100,
        query: str = "",
        knowledge_type: str = "all",
        scope: str = "all",
        source_agent: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
    ) -> dict[str, Any]:
        self._validate_agent_id(requester_agent)
        self._validate_project_key(project_key)
        allowed = {
            "all",
            "candidate",
            "active",
            "stale",
            "archived",
            "quarantined",
        }
        if status not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if knowledge_type != "all" and knowledge_type not in KNOWLEDGE_TYPES:
            raise ValueError(
                f"knowledge_type must be 'all' or one of {sorted(KNOWLEDGE_TYPES)}"
            )
        if scope not in {"all", *SCOPES}:
            raise ValueError("scope must be 'all', 'project' or 'user'")
        if source_agent:
            self._validate_agent_id(source_agent)

        def normalize_bound(value: str | None, *, end: bool = False) -> str | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError("updated_from/updated_to must be ISO timestamps") from error
            if end and len(value) == 10:
                return f"{value}T23:59:59.999999"
            return parsed.isoformat()

        updated_from = normalize_bound(updated_from)
        updated_to = normalize_bound(updated_to, end=True)
        query = (query or "").strip()
        if len(query) > 500:
            raise ValueError("query must be at most 500 characters")

        clauses = []
        parameters: list[Any] = []
        if scope == "user":
            clauses.append("scope = 'user'")
        elif scope == "project":
            clauses.append("scope = 'project' AND project_key = ?")
            parameters.append(project_key)
        else:
            clauses.append("(scope = 'user' OR project_key = ?)")
            parameters.append(project_key)
        if status != "all":
            clauses.append("status = ?")
            parameters.append(status)
        if knowledge_type != "all":
            clauses.append("knowledge_type = ?")
            parameters.append(knowledge_type)
        if source_agent:
            clauses.append("source_agent = ?")
            parameters.append(source_agent)
        if updated_from:
            clauses.append("updated_at >= ?")
            parameters.append(updated_from)
        if updated_to:
            clauses.append("updated_at <= ?")
            parameters.append(updated_to)
        if query:
            escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(title LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR content LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR search_terms LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR source_agent LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR project_key LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parameters.extend([f"%{escaped_query}%"] * 5)

        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM knowledge_records
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return {
            "requester_agent": requester_agent,
            "project_key": project_key,
            "status_filter": status,
            "count": len(rows),
            "results": [
                self._public_record(row, requester_agent, include_content=True)
                for row in rows
            ],
        }

    def overview(
        self,
        *,
        project_key: str,
        agent_ids: list[str],
        limit: int = 1000,
    ) -> dict[str, Any]:
        self._validate_project_key(project_key)
        if not 2 <= len(agent_ids) <= 10:
            raise ValueError("agent_ids must contain between 2 and 10 agents")
        normalized_agents = list(dict.fromkeys(agent_ids))
        if len(normalized_agents) != len(agent_ids):
            raise ValueError("agent_ids must be unique")
        for agent_id in normalized_agents:
            self._validate_agent_id(agent_id)
        if not 1 <= limit <= 2000:
            raise ValueError("limit must be between 1 and 2000")

        with self._connect() as connection:
            project_rows = connection.execute(
                """
                SELECT project_key, COUNT(*) AS count
                FROM knowledge_records
                GROUP BY project_key
                ORDER BY MAX(updated_at) DESC
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT * FROM knowledge_records
                WHERE scope = 'user' OR project_key = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (project_key, limit),
            ).fetchall()
            record_ids = [row["id"] for row in rows]
            evidence_rows: list[sqlite3.Row] = []
            if record_ids:
                placeholders = ", ".join("?" for _ in record_ids)
                evidence_rows = connection.execute(
                    f"""
                    SELECT knowledge_id, agent_id, outcome, evidence_kind
                    FROM knowledge_evidence
                    WHERE knowledge_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    record_ids,
                ).fetchall()

        evidence_by_record: dict[str, list[sqlite3.Row]] = {}
        for evidence in evidence_rows:
            evidence_by_record.setdefault(evidence["knowledge_id"], []).append(evidence)

        records: list[dict[str, Any]] = []
        status_counts = {
            "active": 0,
            "candidate": 0,
            "stale": 0,
            "archived": 0,
            "quarantined": 0,
        }
        source_counts: dict[str, int] = {}
        retrievable_counts: dict[str, int] = {agent_id: 0 for agent_id in normalized_agents}
        available_shared = 0
        confirmed_shared = 0
        for row in rows:
            record = self._public_record(
                row, normalized_agents[0], include_content=True
            )
            evidence = evidence_by_record.get(row["id"], [])
            evidence_agents = sorted({item["agent_id"] for item in evidence})
            confirmed_agents = sorted(
                {
                    item["agent_id"]
                    for item in evidence
                    if item["outcome"] in {"used", "verified", "confirmed_duplicate"}
                }
            )
            participating_agents = set(confirmed_agents) | {row["source_agent"]}
            retrievable = row["status"] in {"active", "stale"} and not versions.blocked(row)
            recall_access = {
                agent_id: retrievable for agent_id in normalized_agents
            }
            if retrievable:
                for agent_id in normalized_agents:
                    retrievable_counts[agent_id] += 1
            both_confirmed = set(normalized_agents).issubset(participating_agents)
            if retrievable and both_confirmed:
                sharing_state = "confirmed_shared"
                confirmed_shared += 1
            elif retrievable:
                sharing_state = "available_shared"
                available_shared += 1
            elif row['status'] in {'active','stale'} and versions.blocked(row):
                sharing_state = 'version_conflict'
            elif row["status"] == "candidate":
                sharing_state = "candidate"
            elif row["status"] == "archived":
                sharing_state = "archived"
            else:
                sharing_state = "quarantined"

            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            source_counts[row["source_agent"]] = (
                source_counts.get(row["source_agent"], 0) + 1
            )
            record.update(
                {
                    "evidence_agents": evidence_agents,
                    "confirmed_agents": confirmed_agents,
                    "recall_access": recall_access,
                    "sharing_state": sharing_state,
                    "evidence_count": len(evidence),
                }
            )
            records.append(record)

        return {
            "project_key": project_key,
            "agent_ids": normalized_agents,
            "projects": [dict(row) for row in project_rows],
            "summary": {
                "total": len(records),
                "status_counts": status_counts,
                "source_counts": source_counts,
                "retrievable_counts": retrievable_counts,
                "available_shared": available_shared,
                "confirmed_shared": confirmed_shared,
            },
            "results": records,
        }

    # ------------------------------------------------------------------
    # Agent registry. Detection is supplied by agent_registry.discover_agents;
    # this table records only the user's explicit framework registrations and
    # the last non-secret discovery metadata.
    # ------------------------------------------------------------------
    def list_agents(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_registry"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY display_name COLLATE NOCASE ASC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._public_agent(row) for row in rows]

    def register_agent(
        self,
        *,
        agent_id: str,
        display_name: str,
        adapter_type: str,
        installed: bool = False,
        detected_by: list[str] | None = None,
        executable_path: str | None = None,
        config_path: str | None = None,
        capabilities: list[str] | None = None,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        self._validate_agent_id(agent_id)
        display_name = self._required_text(display_name, "display_name", 120)
        adapter_type = self._required_text(adapter_type, "adapter_type", 80)
        now = self.clock()
        detected = list(dict.fromkeys(detected_by or []))
        capability_list = list(dict.fromkeys(capabilities or ["shared-knowledge"]))
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT first_registered_at FROM agent_registry WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            first_registered = existing["first_registered_at"] if existing else now
            connection.execute(
                """
                INSERT INTO agent_registry
                    (agent_id, display_name, adapter_type, enabled, installed,
                     detected_by_json, executable_path, config_path,
                     capabilities_json, first_registered_at, last_checked_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    adapter_type = excluded.adapter_type,
                    enabled = 1,
                    installed = excluded.installed,
                    detected_by_json = excluded.detected_by_json,
                    executable_path = excluded.executable_path,
                    config_path = excluded.config_path,
                    capabilities_json = excluded.capabilities_json,
                    last_checked_at = excluded.last_checked_at,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id, display_name, adapter_type, int(installed),
                    json.dumps(detected, ensure_ascii=False), executable_path,
                    config_path, json.dumps(capability_list, ensure_ascii=False),
                    first_registered, checked_at or now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._public_agent(row)

    def update_agent_discovery(self, agent: dict[str, Any]) -> dict[str, Any] | None:
        agent_id = str(agent.get("agent_id") or "")
        self._validate_agent_id(agent_id)
        now = self.clock()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if existing is None:
                return None
            connection.execute(
                """
                UPDATE agent_registry
                SET installed = ?, detected_by_json = ?, executable_path = ?,
                    config_path = ?, capabilities_json = ?, last_checked_at = ?,
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    int(bool(agent.get("installed"))),
                    json.dumps(agent.get("detected_by") or [], ensure_ascii=False),
                    agent.get("executable_path"), agent.get("config_path"),
                    json.dumps(agent.get("capabilities") or [], ensure_ascii=False),
                    agent.get("checked_at") or now, now, agent_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._public_agent(row)

    def disable_agent(self, agent_id: str) -> dict[str, Any]:
        self._validate_agent_id(agent_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_registry SET enabled = 0, updated_at = ? WHERE agent_id = ?",
                (self.clock(), agent_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("agent is not registered")
            row = connection.execute(
                "SELECT * FROM agent_registry WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._public_agent(row)

    def agent_allowed(self, agent_id: str, *, require_registered: bool = False) -> bool:
        with self._connect() as connection:
            row = connection.execute('SELECT enabled FROM agent_registry WHERE agent_id=?', (agent_id,)).fetchone()
        return bool(row['enabled']) if row else not require_registered

    def assert_agent_allowed(self, agent_id: str) -> None:
        if not self.agent_allowed(agent_id):
            raise ValueError('agent is disabled')

    def latest_learning(self, agent_id: str) -> dict[str, Any] | None:
        """Return the latest learning run for an Agent without transcript data."""
        self._validate_agent_id(agent_id)
        with self._connect() as connection:
            try:
                row = connection.execute(
                    """
                    SELECT status, proposal_count, promoted_count, error,
                           created_at, completed_at, latency_ms
                    FROM learning_runs
                    WHERE agent_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (agent_id,),
                ).fetchone()
            except sqlite3.OperationalError as error:
                if "no such table" not in str(error).lower():
                    raise
                row = None
        if row is None:
            return None
        return {
            "status": row["status"],
            "proposal_count": int(row["proposal_count"] or 0),
            "promoted_count": int(row["promoted_count"] or 0),
            "error": row["error"] or "",
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "latency_ms": round(float(row["latency_ms"] or 0), 2),
        }

    @staticmethod
    def _public_agent(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agent_id": row["agent_id"],
            "display_name": row["display_name"],
            "adapter_type": row["adapter_type"],
            "enabled": bool(row["enabled"]),
            "installed": bool(row["installed"]),
            "detected_by": json.loads(row["detected_by_json"] or "[]"),
            "executable_path": row["executable_path"],
            "config_path": row["config_path"],
            "capabilities": json.loads(row["capabilities_json"] or "[]"),
            "first_registered_at": row["first_registered_at"],
            "last_checked_at": row["last_checked_at"],
            "updated_at": row["updated_at"],
        }

    def get(
        self,
        *,
        requester_agent: str,
        knowledge_id: str,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        self._validate_agent_id(requester_agent)
        if project_key is not None:
            self._validate_project_key(project_key)
        with self._connect() as connection:
            record = connection.execute(
                "SELECT * FROM knowledge_records WHERE id = ?"
                + (
                    " AND (scope = 'user' OR project_key = ?)"
                    if project_key is not None
                    else ""
                ),
                (knowledge_id, project_key)
                if project_key is not None
                else (knowledge_id,),
            ).fetchone()
            if record is None:
                raise ValueError("knowledge record not found")
            evidence = connection.execute(
                """
                SELECT agent_id, outcome, summary, evidence_kind, evidence_ref,
                       status_before, status_after, created_at
                FROM knowledge_evidence
                WHERE knowledge_id = ?
                ORDER BY created_at ASC
                """,
                (knowledge_id,),
            ).fetchall()
            version_details = versions.details(connection, record)
        return {
            "knowledge": self._public_record(
                record, requester_agent, include_content=True
            ),
            "evidence": [dict(row) for row in evidence],
            "versions": version_details,
        }

    def remove_many(self, *, agent_id: str, project_key: str,
                    knowledge_ids: list[str]) -> dict[str, Any]:
        """Explicit permanent removal, including indexes and replayable caches.

        Historical event/turn metadata remains an audit trail, not a source for
        recall. This is deliberately separate from reversible retirement.
        """
        self._validate_agent_id(agent_id)
        self._validate_project_key(project_key)
        if not isinstance(knowledge_ids, list) or not 1 <= len(knowledge_ids) <= 100:
            raise ValueError("select between 1 and 100 knowledge records")
        if any(not isinstance(k, str) or not re.fullmatch(r"kn_[A-Za-z0-9]+", k)
               for k in knowledge_ids):
            raise ValueError("invalid knowledge id")
        ids = list(dict.fromkeys(knowledge_ids))
        slots = ','.join('?' for _ in ids)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f'SELECT * FROM knowledge_records WHERE id IN ({slots})', ids).fetchall()
            # Validate the complete batch before making any mutation.
            if any(r['scope'] != 'user' and r['project_key'] != project_key for r in rows):
                raise ValueError('knowledge record outside current project')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            found = {r['id'] for r in rows}
            if not found:
                return {'status': 'removed', 'removed_count': 0, 'already_missing_count': len(ids)}
            for table in ('knowledge_evidence', 'experience_outcomes', 'lifecycle_audit',
                          'shadow_hits', 'knowledge_event_links', 'knowledge_compilation_links', 'knowledge_fts'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE knowledge_id IN ({slots})', ids)
            if 'reuse_traces' in tables:
                traces = db.execute(f'''SELECT DISTINCT t.id,t.items_json FROM reuse_traces t,
                    json_each(t.items_json) i WHERE json_extract(i.value,'$.knowledge_id') IN ({slots})
                    OR json_extract(i.value,'$.related_to') IN ({slots})''', ids + ids).fetchall()
                for trace in traces:
                    items = json.loads(trace['items_json'])
                    for item in items:
                        if item['knowledge_id'] in found:
                            item.update(removed=True, experience=None, check_spec=None, before=None)
                    # Whole cached context is invalidated: retaining even a sibling
                    # heading could repeat removed content on a duplicate hook.
                    db.execute('UPDATE reuse_traces SET context_text=?,items_json=? WHERE id=?',
                               ('', json.dumps(items, ensure_ascii=False), trace['id']))
            db.execute(f'DELETE FROM knowledge_records WHERE id IN ({slots})', ids)
            # A removed successor does not authorize resurrection of its old
            # predecessor: keep superseded_by as an opaque tombstone reference.
            for row in rows:
                versions.refresh(db,row)
            # Rebuild affected derived vocabulary graphs inside this transaction.
            # Decrementing old counts is unsafe: neighbour term selection may change.
            for project in {r['project_key'] for r in rows}:
                db.execute('DELETE FROM term_cooccurrence WHERE project_key=?', (project,))
                remaining = db.execute('SELECT title,search_terms,created_at FROM knowledge_records '
                                       'WHERE project_key=? ORDER BY created_at,rowid', (project,)).fetchall()
                for record in remaining:
                    self._record_term_bridges(db, project_key=project, title=record['title'],
                        search_terms=record['search_terms'] or '', timestamp=record['created_at'])
        return {'status': 'removed', 'removed_count': len(found),
                'already_missing_count': len(ids) - len(found)}

    def feedback(
        self,
        *,
        agent_id: str,
        knowledge_id: str,
        outcome: str,
        evidence_summary: str,
        evidence_kind: str | None = None,
        evidence_ref: str | None = None,
        project_key: str | None = None,
        require_no_related: bool = False,
        supersedes: list[str] | None = None,
    ) -> dict[str, Any]:
        self._validate_agent_id(agent_id)
        if project_key is not None:
            self._validate_project_key(project_key)
        if outcome not in FEEDBACK_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(FEEDBACK_OUTCOMES)}")
        evidence_summary = self._required_text(
            evidence_summary, "evidence_summary", 1000
        )
        evidence_kind = (evidence_kind or "observation").strip()
        if evidence_kind not in EVIDENCE_KINDS:
            raise ValueError(f"evidence_kind must be one of {sorted(EVIDENCE_KINDS)}")
        evidence_ref = self._optional_text(evidence_ref, "evidence_ref", 1000) or ""
        if outcome in {"verified", "rejected"}:
            if evidence_kind not in OBJECTIVE_EVIDENCE_KINDS:
                raise ValueError(
                    "verified/rejected feedback requires objective evidence_kind: "
                    f"one of {sorted(OBJECTIVE_EVIDENCE_KINDS)}"
                )
            if not evidence_ref:
                raise ValueError(
                    "verified/rejected feedback requires evidence_ref pointing to the "
                    "test, command result, approval, or artifact"
                )
        timestamp = self.clock()
        if supersedes is not None and (not isinstance(supersedes,list) or len(supersedes)>100
                or any(not isinstance(k,str) for k in supersedes) or len(set(supersedes))!=len(supersedes)):
            raise ValueError('Invalid replacement selection')
        if supersedes and (outcome!='verified' or evidence_kind!='user_approval' or require_no_related):
            raise ValueError('Replacement requires explicit human approval')

        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            record = connection.execute(
                "SELECT * FROM knowledge_records WHERE id = ?"
                + (
                    " AND (scope = 'user' OR project_key = ?)"
                    if project_key is not None
                    else ""
                ),
                (knowledge_id, project_key)
                if project_key is not None
                else (knowledge_id,),
            ).fetchone()
            if record is None:
                raise ValueError("knowledge record not found")

            if outcome=='verified' and record['superseded_by']:
                raise ValueError('这条知识已被替代，不能直接重新批准；请提交新的候选。')
            conflicts = versions.peers(connection,record) if outcome=='verified' else []
            if conflicts and not require_no_related and set(supersedes or []) != {r['id'] for r in conflicts}:
                raise ValueError('同一事项存在不同的已采纳值，请打开详情核对后选择“采纳并替代旧版”。')
            if supersedes and set(supersedes) != {r['id'] for r in conflicts}:
                raise ValueError('冲突列表已经变化，请刷新详情重新确认。')
            if outcome=='verified' and record['claim_topic'] and record['claim_fingerprint']!=versions.fingerprint(record):
                raise ValueError('知识原文已改变，请重新提交候选。')
            if any(old['claim_fingerprint'] != versions.fingerprint(old) for old in conflicts):
                raise ValueError('冲突知识的原文已改变，请先重新核对原文与版本索引。')

            if require_no_related:
                if outcome != 'verified' or evidence_kind != 'user_approval':
                    raise ValueError('Automatic admission requires an explicit user statement')
                match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in retrieval_tokens(record['title']))
                related = connection.execute('''SELECT 1 FROM knowledge_fts f
                    JOIN knowledge_records r ON r.id=f.knowledge_id
                    WHERE knowledge_fts MATCH ? AND r.id<>? AND r.status IN ('active','stale')
                      AND (r.scope='user' OR r.project_key=?) LIMIT 1''',
                    (match, knowledge_id, record['project_key'])).fetchone() if match else True
                if record['status'] != 'candidate' or related or conflicts:
                    return {'status':'needs_review', 'outcome':outcome,
                            'knowledge':self._public_record(record, agent_id),
                            'transition':{'from':record['status'],'to':record['status']}}

            if outcome == 'verified' and evidence_kind == 'user_approval' and record['status'] == 'active' and not conflicts and not supersedes:
                return {'status': 'already_active', 'outcome': outcome,
                        'transition': {'from': 'active', 'to': 'active'},
                        'knowledge': self._public_record(record, agent_id)}

            adopted_delta = 1 if outcome in {"used", "verified"} else 0
            verified_delta = 1 if outcome == "verified" else 0
            rejected_delta = 1 if outcome == "rejected" else 0
            status_before = record["status"]
            new_verified = record["verified_count"] + verified_delta
            new_rejected = record["rejected_count"] + rejected_delta
            # Usage is telemetry, never permission to resurrect a retired item.
            status = status_before
            if outcome == 'rejected':
                status = 'quarantined'
            elif outcome == 'verified':
                if status_before in {'candidate', 'active', 'stale'} or evidence_kind == 'user_approval':
                    status = 'active'

            # Replacement and activation share one writer transaction. No query
            # can observe half of a confirmed version switch.
            for old in conflicts:
                connection.execute('''UPDATE knowledge_records SET status='archived',superseded_by=?,
                    valid_until=?,updated_at=? WHERE id=?''',(knowledge_id,timestamp,timestamp,old['id']))
                connection.execute('''INSERT INTO lifecycle_audit
                    (id,knowledge_id,from_status,to_status,reason,actor,created_at) VALUES (?,?,?,?,?,?,?)''',
                    ('lc_'+uuid.uuid4().hex[:16],old['id'],old['status'],'archived',
                     'Explicitly replaced by an approved version',agent_id,timestamp))
                connection.execute('DELETE FROM shadow_hits WHERE knowledge_id=?',(old['id'],))
            if status=='active' and status_before!='active':
                connection.execute('UPDATE knowledge_records SET valid_from=?,valid_until=NULL WHERE id=?',
                                   (timestamp,knowledge_id))

            connection.execute(
                """
                UPDATE knowledge_records
                SET adopted_count = adopted_count + ?,
                    verified_count = verified_count + ?,
                    rejected_count = rejected_count + ?,
                    status = ?,
                    candidate_expires_at = CASE WHEN ? = 'candidate' THEN candidate_expires_at ELSE NULL END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    adopted_delta,
                    verified_delta,
                    rejected_delta,
                    status,
                    status,
                    timestamp,
                    knowledge_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_evidence (
                    id, knowledge_id, agent_id, outcome, summary,
                    evidence_kind, evidence_ref, status_before, status_after,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ev_{uuid.uuid4().hex[:16]}",
                    knowledge_id,
                    agent_id,
                    outcome,
                    evidence_summary,
                    evidence_kind,
                    evidence_ref,
                    status_before,
                    status,
                    timestamp,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM knowledge_records WHERE id = ?", (knowledge_id,)
            ).fetchone()
            versions.refresh(connection,updated)
            updated = connection.execute('SELECT * FROM knowledge_records WHERE id=?',(knowledge_id,)).fetchone()

        return {
            "status": "recorded",
            "outcome": outcome,
            "transition": {"from": status_before, "to": status},
            "knowledge": self._public_record(updated, agent_id),
        }

    @staticmethod
    def _required_text(value: str, field: str, maximum: int) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{field} is required")
        if len(value) > maximum:
            raise ValueError(f"{field} exceeds {maximum} characters")
        return value

    @staticmethod
    def _optional_text(
        value: str | None, field: str, maximum: int
    ) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > maximum:
            raise ValueError(f"{field} exceeds {maximum} characters")
        return value

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise ValueError("invalid agent identity")

    @staticmethod
    def _validate_project_key(project_key: str) -> None:
        if not PROJECT_KEY_PATTERN.fullmatch(project_key):
            raise ValueError("invalid project key")

    @staticmethod
    def _public_record(
        row: sqlite3.Row | None,
        requester_agent: str,
        *,
        include_content: bool = False,
    ) -> dict[str, Any]:
        if row is None:
            raise RuntimeError("knowledge record was not persisted")
        result: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "knowledge_type": row["knowledge_type"],
            "scope": row["scope"],
            "project_key": row["project_key"],
            "source_agent": row["source_agent"],
            "source_session": row["source_session"],
            "evidence_speaker": (
                row["evidence_speaker"] if "evidence_speaker" in row.keys() else None
            ),
            "subject_terms": normalize_subject_terms(
                row["subject_terms"] if "subject_terms" in row.keys() else None
            ),
            "search_terms": row["search_terms"],
            "cross_agent": row["source_agent"] != requester_agent,
            "status": row["status"],
            "adopted_count": row["adopted_count"],
            "verified_count": row["verified_count"],
            "rejected_count": row["rejected_count"],
            "candidate_expires_at": (
                row["candidate_expires_at"]
                if "candidate_expires_at" in row.keys()
                else None
            ),
            "confidence": KnowledgeStore._confidence(row),
            "hit_count": row["hit_count"] if "hit_count" in row.keys() else 0,
            "last_hit_at": (
                row["last_hit_at"] if "last_hit_at" in row.keys() else None
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            **{key:row[key] for key in ('claim_topic','claim_value','claim_fingerprint','claim_conflicted',
                                        'superseded_by','valid_from','valid_until') if key in row.keys()},
        }
        if include_content:
            result["content"] = row["content"]
        return result

    @staticmethod
    def _confidence(row: sqlite3.Row) -> float:
        verified = int(row["verified_count"])
        rejected = int(row["rejected_count"])
        total = verified + rejected
        return round(verified / total, 3) if total else 0.0
