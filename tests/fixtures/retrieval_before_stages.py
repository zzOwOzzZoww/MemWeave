"""Frozen pre-stage retrieval oracle, 2026-09-24. Test/benchmark use only."""
import sqlite3
from typing import Any
from agent_knowledge_bridge.store import *
from agent_knowledge_bridge.store import _whole_terms, _bridge_terms


class LegacyKnowledgeStore(KnowledgeStore):
    def search(
        self,
        *,
        requester_agent: str,
        project_key: str,
        query: str,
        limit: int,
        include_retired: bool = False,
        expand_siblings: bool = True,
    ) -> dict[str, Any]:
        """Retrieve knowledge for one query.

        `include_retired` lets a caller see records that retirement removed from
        ordinary retrieval. It exists so the governance layer can ask a
        counterfactual question offline — "would this retired record have been
        served had it stayed active?" — instead of guessing. Callers that are
        serving an agent leave it False.

        `expand_siblings` widens a hit into the cluster of records about the same
        subject, so that knowing *one* thing about a topic implies the rest of it.
        It is on for ordinary retrieval and off for counterfactual measurement,
        which must observe exactly what the unexpanded ranking served.
        """
        self._validate_agent_id(requester_agent)
        self._validate_project_key(project_key)
        query = self._required_text(query, "query", 500)
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        statuses = (
            "('active', 'stale', 'archived')" if include_retired else "('active', 'stale')"
        )
        # A shadow probe is a counterfactual, so it must rank as if retirement had
        # never happened; demoting archived rows here would hide the very records
        # the probe exists to find. Normal retrieval keeps retirement demotions.
        # The prefix is dropped entirely rather than neutralised with a constant,
        # because SQLite reads a bare integer in ORDER BY as a column ordinal.
        status_rank = (
            ""
            if include_retired
            else "CASE WHEN r.status = 'archived' THEN 2 "
            "WHEN r.status = 'stale' THEN 1 ELSE 0 END,"
        )

        tokens = retrieval_tokens(query)
        match_expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
        )
        # Retrieve a wider pool than requested: a record that names the query's
        # distinctive identifier may carry a poor raw BM25 rank, so the pool must
        # reach it before the discriminative re-rank below can lift it.
        pool = max(limit * 8, 24)
        rows: list[sqlite3.Row] = []
        # Records reached by inferring the subject, rather than by matching the
        # query. Empty whenever expansion is off or found nothing, so callers can
        # read it unconditionally.
        sibling_ids: dict[str, str] = {}
        # Records reached only after the query was restated in the corpus's own
        # vocabulary. Tracked apart from both direct hits and siblings because
        # the query did not match them as asked: attributing them to recall
        # would report a lookup the caller never performed. Deliberately a set
        # and not a mapping: a sibling has a real anchor -- the direct hit whose
        # subject it shares -- but a bridged record was reached by *the query
        # itself* being restated, so there is no second record to point at and
        # inventing one would misreport why it appeared.
        bridged: set[str] = set()
        # Records pulled in by naming the same rare term as the query but
        # ranking below the cut. Kept apart from ``bridged`` because the two
        # describe opposite failures: a bridged record was missing the caller's
        # vocabulary, while an anchored one shares a term with the query and
        # simply lost the ranking to records that repeat more of its wording.
        anchored_ids: set[str] = set()
        with self._connect() as connection:
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
                rows = self._pin_discriminative(
                    connection,
                    rows,
                    query=query,
                    limit=limit,
                    status_aware=not include_retired,
                )
                # A result set the query could not fill is the one signal that
                # the caller's wording is the problem rather than the ranking.
                # This is the only path that costs a second FTS pass, and the
                # gate is the caller's own ``limit`` rather than a floor of its
                # own: a request for fewer records than it asked for is the case
                # where a restatement can only add, and a request that came back
                # full has nothing to gain from one. A fixed floor cannot
                # work here -- see the note on ``BRIDGE_SLACK``.
                if expand_siblings and len(rows) < limit:
                    found = self._expand_vocabulary(
                        connection,
                        query=query,
                        project_key=project_key,
                        statuses=statuses,
                        seen={row["id"] for row in rows},
                    )
                    bridged.update(row["id"] for row in found)
                    rows = rows + found
                # Widen before truncating: a record that pinned its way to the
                # top is the best available statement of what the caller is
                # asking about, so the records sharing its subject matter belong
                # in the same answer even when BM25 ranked them far below the
                # cut. Truncating first would discard them before this can run.
                if expand_siblings:
                    rows, sibling_ids = self._expand_siblings_flagged(
                        connection,
                        rows,
                        limit=limit,
                        statuses=statuses,
                        project_key=project_key,
                        include_retired=include_retired,
                    )
                rows = rows[:limit]
                # The complement of the pass above, and it runs after the
                # truncation on purpose. When the direct hits fill every slot the
                # bridge is skipped and the page the caller reads is exactly what
                # BM25 ranked -- the other way a subject is lost. A cluster's
                # remaining records are usually ordinary matches that lost the
                # ranking, not records missing the vocabulary, so they are reached
                # by their shared rare term and appended *past* the caller's
                # slots. Nothing is displaced: the direct hits keep the page they
                # earned, and the anchor adds the unfilled remainder of the page
                # plus ``BRIDGE_SLACK`` beyond it, never more.
                if expand_siblings:
                    anchored = self._expand_anchored(
                        connection,
                        query=query,
                        project_key=project_key,
                        statuses=statuses,
                        room=limit + BRIDGE_SLACK - len(rows),
                        seen={row["id"] for row in rows} | set(sibling_ids),
                    )
                    anchored_ids.update(row["id"] for row in anchored)
                    rows = rows + anchored
            if not rows:
                like_query = f"%{query}%"
                rows = connection.execute(
                    f"""
                    SELECT r.*, 0.0 AS rank
                    FROM knowledge_records r
                    WHERE r.status IN {statuses}
                      AND (r.scope = 'user' OR r.project_key = ?)
                      AND (r.title LIKE ? OR r.content LIKE ?)
                    ORDER BY {status_rank} r.verified_count DESC,
                             r.updated_at DESC
                    LIMIT ?
                    """,
                    (project_key, like_query, like_query, limit),
                ).fetchall()

        return {
            "query": query,
            "requester_agent": requester_agent,
            "project_key": project_key,
            "count": len(rows),
            "results": [
                {**self._public_record(row, requester_agent, include_content=True),
                 "retrieval_score": row["rank"], "retrieval_method": "fts5-enriched",
                 # "direct" means the query matched this record; "sibling" means
                 # it was pulled in because it is about the same subject as one
                 # that did. Governance counts recall over direct matches only --
                 # an inferred neighbour is not evidence that retrieval found it.
                 # ``related_to`` names the direct hit a sibling was inferred
                 # from, so a reader can follow the inference back to the record
                 # the query actually reached.
                 # Three origins, and they are not interchangeable. ``direct``
                 # means the query matched this record as the caller wrote it;
                 # ``sibling`` means it is about the same subject as one that
                 # did; ``bridged`` means it was found only after the query was
                 # restated in terms the corpus itself associates with the
                 # caller's vocabulary. Only the first is evidence that retrieval
                 # found the record, so governance counts recall over it alone.
                 #
                 # Only a sibling carries ``related_to``. A bridged record was
                 # reached by the query's own restatement, not by way of another
                 # record, so there is no anchor to name and reporting one would
                 # misstate how it was found.
                 "origin": (
                     "bridged" if row["id"] in bridged
                     else "anchored" if row["id"] in anchored_ids
                     else "sibling" if row["id"] in sibling_ids
                     else "direct"
                 ),
                 **({"related_to": sibling_ids[row["id"]]}
                    if row["id"] in sibling_ids and row["id"] not in bridged
                    else {})}
                for row in rows
            ],
        }


    def _pin_discriminative(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        query: str,
        limit: int,
        status_aware: bool = True,
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
        frequencies = bounded_document_frequencies(
            connection, DISCRIMINATIVE_TOKEN.findall(query), ceiling=limit)
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
        return [row for row in found if row["id"] not in seen]


    def _expand_anchored(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        project_key: str,
        statuses: str,
        room: int,
        seen: set[str],
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
        anchors = anchor_terms(connection, retrieval_tokens(query))
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
        return [row for row in found if row["id"] not in seen][:room]


    def _expand_siblings_flagged(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        limit: int,
        statuses: str,
        project_key: str,
        include_retired: bool,
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

        The result keeps the incoming relevance order and interleaves siblings
        after it, so a caller always sees the literal matches first and the
        surrounding cluster immediately behind them. At least half the slots stay
        reserved for direct matches, so this cannot displace what the query
        actually asked for.
        """
        if not rows or limit < 2 or len(rows) < limit:
            # Fewer direct hits than slots means every slot is already spoken
            # for by a record the query matched outright; widening now would
            # trade a literal match for an inferred one.
            return rows, {}
        topic: set[str] = set()
        for row in rows[:3]:
            topic |= _whole_terms(f"{row['title']} {row['search_terms'] or ''}")
        if len(topic) < MIN_SIBLING_OVERLAP:
            return rows, {}

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
            return rows, {}
        # Rarer shared terms first; the count only breaks ties, so a record
        # sharing one distinctive term outranks one sharing three common ones.
        scored.sort(key=lambda item: (-item[0], -item[1], -int(item[2]["verified_count"])))
        siblings = [candidate for _weight, _count, candidate in scored[:MAX_SIBLINGS]]

        reserved = max(1, (limit + 1) // 2)
        merged = list(rows[:reserved]) + siblings + list(rows[reserved:])
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
        return merged, anchors


