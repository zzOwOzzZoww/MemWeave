"""Candidate generation -> expansion -> arbitration -> truncation.

Stages only append immutable hits. They never reorder/delete another stage's
output. A shared pure policy defines expansion seed views and final placement;
viewing a prefix does not discard candidates. Query matches keep the primary
page; anchors and at most one sibling share its bounded supplementary budget.
"""
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Mapping, Protocol
from agent_knowledge_bridge.experiences import filter_rows, rank_equivalent
from agent_knowledge_bridge.evidence import evidence_support


@dataclass(frozen=True)
class RetrievalPolicy:
    stages: tuple[str, ...] = ('bridge', 'sibling', 'anchor')
    sibling_seed_origins: tuple[str, ...] = ('direct', 'bridged')
    max_depth: int = 2
    slack: int = 2

    def __post_init__(self):
        if len(set(self.stages)) != len(self.stages) or not set(self.stages) <= {'bridge', 'sibling', 'anchor'}:
            raise ValueError('Unknown or repeated expansion stage')
        if not set(self.sibling_seed_origins) <= {'direct', 'bridged'}:
            raise ValueError('Sibling seeds must be direct or bridged')
        if not 0 <= self.max_depth <= 2 or not 0 <= self.slack <= 2:
            raise ValueError('Expansion depth/slack out of bounds')


@dataclass(frozen=True)
class Candidate:
    row: Mapping[str, Any]
    origin: str = 'direct'
    parent_id: str | None = None
    parent_title: str | None = None
    parent_origin: str | None = None
    depth: int = 0

    @property
    def id(self):
        return self.row['id']

    def provenance(self):
        data = {'origin': self.origin, 'depth': self.depth}
        if self.parent_id:
            data.update(parent_id=self.parent_id, parent_origin=self.parent_origin)
        return data


@dataclass(frozen=True)
class StageResult:
    additions: tuple[Candidate, ...] = ()
    skipped_reason: str | None = None


@dataclass(frozen=True)
class StageContext:
    store: Any
    connection: Any
    query: str
    project_key: str
    statuses: str
    limit: int
    include_retired: bool
    policy: RetrievalPolicy
    statistics: Any


class ExpansionStage(Protocol):
    name: str

    def expand(self, context: StageContext, candidates: tuple[Candidate, ...]) -> StageResult: ...


def unique(hits):
    seen, result = set(), []
    for candidate in hits:
        if candidate.id not in seen:
            seen.add(candidate.id)
            result.append(candidate)
    return tuple(result)


def primary_order(candidates, limit):
    base = unique(c for c in candidates if c.origin in {'direct', 'bridged'})
    base_ids = {c.id for c in base}
    siblings = unique(c for c in candidates if c.origin == 'sibling' and c.id not in base_ids)
    return (*base, *siblings)


class BridgeStage:
    name = 'bridge'

    def expand(self, ctx, candidates):
        direct = tuple(c for c in candidates if c.origin == 'direct')
        if len(direct) >= ctx.limit:
            return StageResult(skipped_reason='direct_pool_full')
        rows = ctx.store._expand_vocabulary(ctx.connection, query=ctx.query,
            project_key=ctx.project_key, statuses=ctx.statuses, seen={c.id for c in candidates})
        return StageResult(tuple(Candidate(r, 'bridged', depth=1) for r in rows))


class SiblingStage:
    name = 'sibling'

    def expand(self, ctx, candidates):
        seeds = unique(c for c in candidates if c.origin in ctx.policy.sibling_seed_origins
                       and c.depth < ctx.policy.max_depth)
        if not seeds or ctx.limit < 2 or len(seeds) < ctx.limit:
            return StageResult(skipped_reason='insufficient_seed_pool')
        rows, parents = ctx.store._sibling_additions(ctx.connection, [c.row for c in seeds],
            statuses=ctx.statuses, project_key=ctx.project_key)
        rows = filter_rows(rows, ctx.query)
        by_id = {c.id: c for c in seeds}
        additions = []
        for row in rows:
            parent = by_id[parents[row['id']]]
            additions.append(Candidate(row, 'sibling', parent.id, parent.row['title'],
                                        parent.origin, parent.depth + 1))
        return StageResult(tuple(additions))


class AnchorStage:
    name = 'anchor'

    def expand(self, ctx, candidates):
        # Seed projection only. The full pool is retained through arbitration.
        page = primary_order(candidates, ctx.limit)[:ctx.limit]
        room = ctx.limit + ctx.policy.slack - len(page)
        if room <= 0:
            return StageResult(skipped_reason='no_expansion_budget')
        seen = {c.id for c in page} | {c.id for c in candidates if c.origin == 'sibling'}
        rows = ctx.store._expand_anchored(ctx.connection, query=ctx.query,
            project_key=ctx.project_key, statuses=ctx.statuses, room=room, seen=seen,
            statistics=ctx.statistics)
        return StageResult(tuple(Candidate(r, 'anchored', depth=1) for r in rows))


STAGES = (BridgeStage(), SiblingStage(), AnchorStage())


def expand(ctx, direct, *, enabled=True):
    candidates = tuple(Candidate(row) for row in direct)
    reports = []
    for stage in STAGES:
        before = len(candidates)
        start = perf_counter()
        if not enabled or stage.name not in ctx.policy.stages or ctx.policy.max_depth == 0:
            result = StageResult(skipped_reason='disabled')
        else:
            result = stage.expand(ctx, candidates)
        candidates += result.additions
        reports.append({'stage': stage.name, 'input_count': before,
                        'added_count': len(result.additions), 'skipped_reason': result.skipped_reason,
                        'elapsed_ms': round((perf_counter() - start) * 1000, 3)})
    return candidates, reports


@dataclass(frozen=True)
class ArbitrationResult:
    primary: tuple[Candidate, ...]
    extra: tuple[Candidate, ...]
    fallback: tuple[Candidate, ...]
    provenance: dict[str, list[dict]] = field(default_factory=dict)
    adaptive_changes: int = 0


def arbitrate(candidates, fallback, limit, *, connection=None):
    provenance = {}
    for candidate in candidates:
        paths = provenance.setdefault(candidate.id, [])
        path = candidate.provenance()
        if path not in paths:
            paths.append(path)
    # Preserve legacy attribution if an out-of-page bridge hit is re-added by
    # anchor. Keep both paths in provenance, but do not claim a literal match.
    bridged_ids = {c.id for c in candidates if c.origin == 'bridged'}
    extra = unique(replace(c, origin='bridged') if c.id in bridged_ids else c
                   for c in candidates if c.origin == 'anchored')
    extra_ids = {c.id for c in extra}
    extra += unique(c for c in candidates if c.origin == 'sibling' and c.id not in extra_ids)
    primary = tuple(primary_order(candidates, limit))
    changes = 0
    if connection is not None:
        ordered, changes = rank_equivalent(connection, [c.row for c in primary],
                                          origins={c.id: c.origin for c in primary})
        hits = {c.id: c for c in primary}
        primary = tuple(hits[row['id']] for row in ordered)
    return ArbitrationResult(primary, extra,
        tuple(Candidate(row) for row in fallback), provenance, changes)


def truncate(ranked, *, limit, slack):
    selected = []
    for candidate in ranked.primary:
        if len(selected) >= limit:
            break
        if candidate.origin == 'sibling' and any(c.origin == 'sibling' for c in selected):
            continue
        selected.append(candidate)
    used = {c.id for c in selected}
    for candidate in ranked.extra:
        if len(selected) >= limit + slack:
            break
        if candidate.origin == 'sibling' and any(c.origin == 'sibling' for c in selected):
            continue
        if candidate.id not in used:
            selected.append(candidate)
            used.add(candidate.id)
    if not selected:
        selected = list(ranked.fallback[:limit])
        used = {c.id for c in selected}
    omitted = []
    for candidate in unique((*ranked.primary, *ranked.extra, *ranked.fallback)):
        if candidate.id not in used:
            omitted.append({'knowledge_id': candidate.id,
                            'reason': 'fallback_not_needed' if candidate in ranked.fallback else 'row_budget'})
    return selected, omitted


def order_recovery_candidates(normal, recovered, *, query):
    """Preserve ordinary order; insert archives only before weaker evidence.

    Keep overflow records for emission-time fallback. The caller applies the
    same row/character budget after locked live validation, never at discovery.
    """
    records = list(normal)
    if not recovered:
        return records
    seen = {r['id'] for r in records}
    scores = {r['id']: evidence_support(query, r)[2] for r in records}
    for record in recovered:
        if record['id'] in seen:
            continue
        supported, _, score = evidence_support(query, record)
        if not supported:
            continue
        index = next((i for i, row in enumerate(records) if score > scores[row['id']]), len(records))
        records.insert(index, record)
        scores[record['id']] = score
        seen.add(record['id'])
    return records
