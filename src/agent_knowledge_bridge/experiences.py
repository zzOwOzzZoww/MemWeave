"""Scoped, evidence-bound experience contracts. No command execution or LLM calls.

A contract is a procedure with explicit applicability and a known verifier.
Recorded verifier outcomes are observations, never causal proof of memory use.
"""
from __future__ import annotations

import json
import re

MAX_CONTRACT_CHARS = 6000
PREFIX = '{"experience":'


def _texts(value, name, minimum=0, maximum=8, size=240):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must contain {minimum}..{maximum} strings")
    if any(not isinstance(t, str) or not 2 <= len(t.strip()) <= size for t in value):
        raise ValueError(f"invalid {name}")
    return list(dict.fromkeys(t.strip() for t in value))


def validate_contract(value):
    if not isinstance(value, dict):
        raise ValueError("experience must be an object")
    allowed = {"version", "applies_when", "exclude_when", "steps", "avoid", "reason", "verifier"}
    if set(value) - allowed:
        raise ValueError("unknown experience properties")
    verifier = value.get("verifier")
    if (not isinstance(verifier, dict) or set(verifier) != {"kind", "command"}
            or verifier["kind"] != "observed_command"
            or not isinstance(verifier["command"], str)
            or not 3 <= len(verifier["command"].strip()) <= 500
            or any(c in verifier["command"] for c in "\r\n\x00")):
        raise ValueError("a bounded observed_command verifier is required")
    reason = value.get("reason", "")
    if not isinstance(reason, str) or not 2 <= len(reason.strip()) <= 500:
        raise ValueError("a bounded explanation is required")
    if value.get("version", 1) != 1:
        raise ValueError("unsupported experience version")
    contract = {
        "version": 1,
        "applies_when": _texts(value.get("applies_when"), "applies_when", 1, 6, 100),
        "exclude_when": _texts(value.get("exclude_when", []), "exclude_when", 0, 6, 100),
        "steps": _texts(value.get("steps"), "steps", 1, 6, 300),
        "avoid": _texts(value.get("avoid", []), "avoid", 0, 4, 240),
        "reason": reason.strip(),
        "verifier": {"kind": "observed_command", "command": verifier["command"].strip()},
    }
    encoded = json.dumps(contract, ensure_ascii=False)
    if len(encoded) > MAX_CONTRACT_CHARS:
        raise ValueError("experience is too large")
    # Local import is pure and does not pull in storage or adapters.
    from agent_knowledge_bridge.claude_transcript import redact_text
    if redact_text(encoded, len(encoded) + 1) != encoded:
        raise ValueError("experience contains a credential")
    return contract


def encode_contract(value):
    return json.dumps({"experience": validate_contract(value)}, ensure_ascii=False, separators=(",", ":"))


def contract_from_content(content):
    if not isinstance(content, str) or not content.startswith(PREFIX):
        return None
    try:
        return validate_contract(json.loads(content)["experience"])
    except (ValueError, TypeError, KeyError):
        return None


def phrase_in(phrase, text):
    phrase, text = phrase.casefold(), text.casefold()
    # Latin words/identifiers must not match inside unrelated words.
    if re.fullmatch(r"[a-z0-9_. /:-]+", phrase):
        return re.search(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", text) is not None
    return phrase in text


def applicable(contract, query):
    return (all(phrase_in(term, query) for term in contract["applies_when"])
            and not any(phrase_in(term, query) for term in contract["exclude_when"]))


def filter_rows(rows, query):
    result = []
    for row in rows:
        content = row['content'] if 'content' in row.keys() else ''
        if isinstance(content, str) and content.startswith(PREFIX):
            contract = contract_from_content(content)
            if not contract or not applicable(contract, query):
                continue
        result.append(row)
    return result


def display_contract(contract):
    lines = [
        "经验适用条件（全部满足）：" + "；".join(contract["applies_when"]),
        "不适用：" + ("；".join(contract["exclude_when"]) or "未声明额外排除条件"),
        "已观察到的教训：" + contract["reason"],
        "执行检查清单：",
        *[f"{i}. {step}" for i, step in enumerate(contract["steps"], 1)],
    ]
    if contract["avoid"]:
        lines.append("避免：" + "；".join(contract["avoid"]))
    lines.extend([
        "完成后验证：" + contract["verifier"]["command"],
        "仅在当前任务授权与环境适用时执行；不得跳过验证或将本条经验当作已验证结果。",
    ])
    return "\n".join(lines)


def command_from_summary(summary):
    try:
        value = json.loads(summary)
        if isinstance(value, dict):
            command = value.get("command") or value.get("cmd")
            return command.strip() if isinstance(command, str) else ""
    except (ValueError, TypeError):
        pass
    return summary.strip() if isinstance(summary, str) else ""


def admission_evidence(contract, event_map, available_ids, cited_ids):
    """A specific verifier must fail then pass in this ordered source turn.

    This proves a recovery observation, not that the proposed steps caused it.
    All applicable conditions also need support in the source material; the
    adapter checks that separately against the source turn.
    """
    command = contract["verifier"]["command"]
    matched = [(key, event_map[key]) for key in available_ids
               if key in event_map
               and event_map[key]["objective_kind"] in {"test", "command"}
               and command_from_summary(event_map[key]["payload"].get("input", "")) == command]
    failures = [key for key, event in matched if event["success"] is False and key in cited_ids]
    if (not failures or not matched or matched[-1][1]["success"] is not True
            or matched[-1][0] not in cited_ids):
        return None
    # The pass must occur after a cited failure.
    return {"failed_event": failures[0], "passed_event": matched[-1][0]}


def observe_contract(contract, tools, association):
    if association not in {"turn_id", "trace_citation", "transcript_boundary"}:
        return {"status": "weak_turn_association", "failures": 0, "checks": 0}
    command = contract["verifier"]["command"]
    matched = [t for t in tools if t.objective_kind in {"test", "command"}
               and command_from_summary(t.input_summary) == command]
    if not matched or matched[-1].success is None:
        return {"status": "not_checked", "failures": 0, "checks": 0}
    failures = sum(t.success is False for t in matched)
    status = ("recovered" if failures else "passed") if matched[-1].success is True else "failed"
    return {"status": status, "failures": failures, "checks": len(matched)}


def record_outcome(db, item, *, agent_id, timestamp, trace_id):
    """Update a per-record aggregate inside the trace completion transaction."""
    result = item.get("experience_result")
    if not result:
        return
    status = result["status"]
    if status not in {"passed", "recovered", "failed", "not_checked", "weak_turn_association"}:
        return
    key = item["knowledge_id"]
    # A record may have been retired/edited since injection; its contract version
    # must still match. Historical traces remain valid observations on their own.
    row = db.execute("SELECT content,status FROM knowledge_records WHERE id=?", (key,)).fetchone()
    if not row or contract_from_content(row["content"]) != item.get("experience"):
        return
    passed = status in {"passed", "recovered"}
    failed = status == "failed"
    cross = agent_id != item["source_agent"]
    db.execute("""UPDATE experience_outcomes SET
        observed=observed+1, passed=passed+?, failed=failed+?, recovered=recovered+?,
        unverified=unverified+?, cross_agent_passed=cross_agent_passed+?,
        failure_events=failure_events+?, last_outcome=?, updated_at=?
        WHERE knowledge_id=?""",
        (int(passed), int(failed), int(status == "recovered"), int(not passed and not failed),
         int(passed and cross), result["failures"], status, timestamp, key))
    if failed and row["status"] in {"active", "stale"}:
        # One real counterexample suspends serving even if historical passes
        # outnumber failures. Re-enable only through explicit existing review.
        db.execute("UPDATE knowledge_records SET status='quarantined',updated_at=? WHERE id=?",
                   (timestamp, key))
        db.execute("""INSERT INTO lifecycle_audit
            (id,knowledge_id,from_status,to_status,reason,actor,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            ("ex_" + trace_id + "_" + key, key, row["status"], "quarantined",
             "Experience verifier failed after injection; review applicability before reuse",
             agent_id, timestamp))
        db.execute("""DELETE FROM lifecycle_audit WHERE knowledge_id=? AND id NOT IN
            (SELECT id FROM lifecycle_audit WHERE knowledge_id=? ORDER BY created_at DESC,rowid DESC LIMIT 50)""",
            (key, key))


def metrics(knowledge, project_key):
    with knowledge._connect() as db:
        row = db.execute("""SELECT count(*) AS total,
            sum(CASE WHEN r.status='candidate' THEN 1 ELSE 0 END) AS candidates,
            sum(CASE WHEN r.status IN ('active','stale') THEN 1 ELSE 0 END) AS ready,
            sum(CASE WHEN r.status='quarantined' THEN 1 ELSE 0 END) AS suspended,
            sum(o.passed) AS passed,sum(o.failed) AS failed,sum(o.recovered) AS recovered,
            sum(o.cross_agent_passed) AS cross_agent_passed,sum(o.unverified) AS unverified,
            sum(o.failure_events) AS failure_events
            FROM experience_outcomes o JOIN knowledge_records r ON r.id=o.knowledge_id
            WHERE r.project_key=? OR r.scope='user'""", (project_key,)).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}


def rank_equivalent(db, rows, origins=None):
    """Prefer demonstrated transfers only within identical applicability groups.

    No cross-topic reranking: positions occupied by ordinary knowledge or a
    different contract are untouched. Bounded candidates, one keyed SQL read.
    A Laplace-smoothed success estimate is a ranking heuristic, not confidence.
    """
    groups = {}
    for index, row in enumerate(rows):
        contract = contract_from_content(row['content'])
        if contract:
            key = (tuple(sorted(t.casefold() for t in contract['applies_when'])),
                   tuple(sorted(t.casefold() for t in contract['exclude_when'])),
                   contract['verifier']['command'], row['status'], row['project_key'], row['scope'],
                   (origins or {}).get(row['id'], 'direct'))
            groups.setdefault(key, []).append((index, row))
    competing = [group for group in groups.values() if len(group) > 1]
    if not competing:
        return rows, 0
    ids = [r['id'] for group in competing for _, r in group]
    stats = {r['knowledge_id']: r for r in db.execute(
        'SELECT knowledge_id,passed,failed,cross_agent_passed FROM experience_outcomes WHERE knowledge_id IN ('
        + ','.join('?' for _ in ids) + ')', ids)}
    def score(row):
        s = stats.get(row['id'])
        return ((s['passed'] + 1) / (s['passed'] + s['failed'] + 2),
                min(s['cross_agent_passed'], 3)) if s else (0.5, 0)
    result = list(rows)
    changes = 0
    for group in competing:
        ranked = sorted((r for _, r in group), key=score, reverse=True)
        for (index, original), replacement in zip(group, ranked):
            changes += original['id'] != replacement['id']
            result[index] = replacement
    return result, changes
