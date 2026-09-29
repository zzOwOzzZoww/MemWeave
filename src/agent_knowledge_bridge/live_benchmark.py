"""Deterministic benchmark fixtures and scoring for live MemWeave evaluation."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any


OPERATIONS = ("add", "subtract", "multiply")


def _apply(operation: str, value: int, operand: int) -> int:
    if operation == "add":
        return value + operand
    if operation == "subtract":
        return value - operand
    if operation == "multiply":
        return value * operand
    raise ValueError(f"unsupported operation: {operation}")


def _instruction(operation: str, operand: int) -> str:
    verbs = {
        "add": f"add {operand} to the input integer",
        "subtract": f"subtract {operand} from the input integer",
        "multiply": f"multiply the input integer by {operand}",
    }
    return verbs[operation]


def _policy(
    *,
    key: str,
    marker: str,
    code: str,
    operation: str,
    operand: int,
    project_key: str = "benchmark-main",
    source_agent: str = "claude-code",
    scope: str = "project",
    state: str = "active",
    verification_count: int = 1,
    revision: str = "CURRENT revision 1",
    search_terms: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"{marker} operational policy",
        "content": (
            f"Marker: {marker}. Status: {revision}. Policy code: {code}. "
            f"For this marker, {_instruction(operation, operand)}."
        ),
        "project_key": project_key,
        "source_agent": source_agent,
        "scope": scope,
        "state": state,
        "verification_count": verification_count,
        "search_terms": search_terms,
    }


def _case(
    *,
    case_id: str,
    category: str,
    query: str,
    value: int,
    code: str,
    operation: str,
    operand: int,
    expected_key: str | None,
    project_key: str = "benchmark-main",
    requester_agent: str = "codex",
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "query": query,
        "input": value,
        "expected": {"policy_code": code, "result": _apply(operation, value, operand)},
        "expected_key": expected_key,
        "project_key": project_key,
        "requester_agent": requester_agent,
        "notes": notes,
    }


def build_live_benchmark() -> dict[str, Any]:
    """Build 100 deterministic cases without embedding generated knowledge IDs."""
    knowledge: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    exact_topics = (
        "cache-nova", "queue-ember", "index-orbit", "ledger-pine", "proxy-coral",
        "worker-maple", "token-river", "schema-lotus", "backup-sable", "audit-cinder",
        "stream-cedar", "metric-opal", "router-birch", "socket-amber", "batch-flint",
        "search-ivory", "event-cobalt", "parser-mint", "deploy-ruby", "buffer-spruce",
        "session-topaz", "archive-willow", "mirror-onyx", "filter-lilac", "digest-pearl",
        "client-jasper", "server-quartz", "policy-hazel", "record-indigo", "shard-copper",
        "commit-silver", "branch-crimson", "release-olive", "runtime-violet", "trace-garnet",
    )
    for index, marker in enumerate(exact_topics, 1):
        operation = OPERATIONS[(index - 1) % len(OPERATIONS)]
        operand = 2 + index % 7
        value = 4 + index
        key = f"exact-{index:02d}"
        code = f"MW-E{index:03d}"
        knowledge.append(_policy(key=key, marker=marker, code=code,
                                 operation=operation, operand=operand))
        cases.append(_case(
            case_id=f"E{index:03d}", category="exact_retrieval",
            query=(f"处理任务标记 {marker}，输入整数为 {value}。"
                   "请应用该标记对应的持久策略并返回结果。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=key,
        ))

    paraphrases = (
        ("rotate signing keys before deployment", "部署之前轮换签名密钥"),
        ("compact the audit journal after export", "导出之后压缩审计日志"),
        ("invalidate cached sessions after password reset", "密码重置后清除缓存会话"),
        ("verify backup checksum before restore", "恢复之前校验备份摘要"),
        ("drain the worker queue before shutdown", "关闭服务前排空工作队列"),
        ("refresh service tokens after role change", "角色变更后刷新服务令牌"),
        ("rebuild the search index after migration", "迁移结束后重建搜索索引"),
        ("archive expired records before compaction", "压缩数据前归档过期记录"),
        ("validate schema compatibility before rollout", "发布之前检查数据结构兼容性"),
        ("quarantine malformed events before replay", "回放之前隔离格式错误的事件"),
        ("renew client certificates before expiry", "客户端证书到期前完成续签"),
        ("flush buffered metrics before restart", "重启之前写出缓冲区中的指标"),
        ("reconcile ledger entries after import", "导入之后核对账本记录"),
        ("rotate archive segments after sealing", "封存之后轮换归档分段"),
        ("verify replica lag before failover", "故障切换前检查副本延迟"),
        ("sanitize exported logs before sharing", "共享导出日志前清除敏感内容"),
        ("warm the query cache after startup", "服务启动后预热查询缓存"),
        ("revoke stale grants after review", "评审完成后撤销过期授权"),
        ("deduplicate messages before delivery", "消息投递之前完成去重"),
        ("snapshot runtime state before upgrade", "升级之前保存运行状态快照"),
    )
    for index, (english, chinese) in enumerate(paraphrases, 1):
        operation = OPERATIONS[index % len(OPERATIONS)]
        operand = 3 + index % 5
        value = 20 + index
        key = f"semantic-{index:02d}"
        code = f"MW-S{index:03d}"
        knowledge.append(_policy(key=key, marker=english, code=code,
                                 operation=operation, operand=operand,
                                 search_terms=chinese))
        cases.append(_case(
            case_id=f"S{index:03d}", category="cross_language_paraphrase",
            query=(f"有一项内部计算策略描述为“{chinese}”。输入整数为 {value}。"
                   "请从持久知识中找到对应策略并计算结果。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=key,
            notes="Knowledge is English while the query is a Chinese semantic paraphrase.",
        ))

    for index in range(1, 16):
        marker = f"tenant-shared-marker-{index:02d}"
        operation = OPERATIONS[(index + 1) % len(OPERATIONS)]
        operand = 2 + index % 6
        value = 40 + index
        project = f"target-project-{index:02d}"
        key = f"scope-correct-{index:02d}"
        code = f"MW-P{index:03d}"
        knowledge.append(_policy(key=key, marker=marker, code=code,
                                 operation=operation, operand=operand,
                                 project_key=project))
        knowledge.append(_policy(
            key=f"scope-distractor-{index:02d}", marker=marker,
            code=f"WRONG-P{index:03d}", operation="add", operand=90 + index,
            project_key=f"other-project-{index:02d}",
        ))
        cases.append(_case(
            case_id=f"P{index:03d}", category="project_scope_isolation",
            query=(f"处理任务标记 {marker}，输入整数为 {value}。"
                   "必须使用当前项目的持久策略。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=key, project_key=project,
            notes="A same-marker distractor exists in another project.",
        ))

    for index in range(1, 11):
        marker = f"shared-agent-policy-{index:02d}"
        operation = OPERATIONS[(index + 2) % len(OPERATIONS)]
        operand = 4 + index % 5
        value = 60 + index
        key = f"shared-{index:02d}"
        code = f"MW-A{index:03d}"
        knowledge.append(_policy(
            key=key, marker=marker, code=code, operation=operation, operand=operand,
            project_key="claude-origin", source_agent="claude-code", scope="user",
        ))
        cases.append(_case(
            case_id=f"A{index:03d}", category="cross_agent_user_scope",
            query=(f"处理共享任务标记 {marker}，输入整数为 {value}。"
                   "请使用可共享的持久策略。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=key, project_key="codex-consumer", requester_agent="codex",
            notes="Claude Code sourced user-scope knowledge is consumed by Codex.",
        ))

    for index in range(1, 11):
        marker = f"versioned-policy-{index:02d}"
        operation = OPERATIONS[index % len(OPERATIONS)]
        operand = 3 + index % 7
        value = 80 + index
        key = f"conflict-current-{index:02d}"
        code = f"MW-C{index:03d}"
        knowledge.append(_policy(
            key=key, marker=marker, code=code, operation=operation, operand=operand,
            verification_count=3, revision="CURRENT revision 2; use this revision",
        ))
        stale_state = "active" if index <= 5 else "quarantined"
        knowledge.append(_policy(
            key=f"conflict-old-{index:02d}", marker=marker,
            code=f"OLD-C{index:03d}", operation="add", operand=50 + index,
            state=stale_state, verification_count=1,
            revision="SUPERSEDED revision 1; do not use this revision",
        ))
        cases.append(_case(
            case_id=f"C{index:03d}", category="conflict_and_staleness",
            query=(f"处理版本化任务标记 {marker}，输入整数为 {value}。"
                   "如果存在多个版本，只能使用当前版本。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=key,
            notes=("Old record remains active." if stale_state == "active"
                   else "Old record is quarantined and must not be retrieved."),
        ))

    for index in range(1, 11):
        operation = OPERATIONS[(index - 1) % len(OPERATIONS)]
        operand = 2 + index
        value = 100 + index
        code = f"PUBLIC-N{index:03d}"
        operation_cn = {
            "add": f"加 {operand}",
            "subtract": f"减 {operand}",
            "multiply": f"乘 {operand}",
        }[operation]
        cases.append(_case(
            case_id=f"N{index:03d}", category="irrelevant_no_memory",
            query=(f"这是一个完全自包含的公开计算，不需要持久知识。输入整数为 {value}，"
                   f"对它执行{operation_cn}。策略代码固定为 {code}。"),
            value=value, code=code, operation=operation, operand=operand,
            expected_key=None,
            notes="No knowledge should be retrieved or injected.",
        ))

    if len(cases) != 100:
        raise AssertionError(f"benchmark must contain 100 cases, got {len(cases)}")
    return {
        "version": "memweave-live-100-v1",
        "description": (
            "Controlled causal benchmark: 100 tasks, each evaluated as baseline, "
            "automatic MemWeave recall, and oracle context."
        ),
        "knowledge": knowledge,
        "cases": cases,
    }


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def score_model_output(
    text: str,
    expected: dict[str, Any],
    injected_ids: list[str],
    expected_id: str | None,
) -> dict[str, Any]:
    try:
        parsed = parse_json_object(text)
        parse_error = None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        parsed = {}
        parse_error = f"{type(exc).__name__}: {exc}"
    policy_match = parsed.get("policy_code") == expected["policy_code"]
    result = parsed.get("result")
    result_match = type(result) is type(expected["result"]) and result == expected["result"]
    reported = parsed.get("used_memory_ids", [])
    if not isinstance(reported, list) or not all(isinstance(item, str) for item in reported):
        reported = []
    return {
        "success": bool(policy_match and result_match),
        "parsed": parsed,
        "parse_error": parse_error,
        "policy_match": policy_match,
        "result_match": result_match,
        "reported_memory_ids": reported,
        "expected_memory_reported": expected_id in reported if expected_id else not reported,
        "hallucinated_memory_ids": sorted(set(reported) - set(injected_ids)),
    }


def extended_paired_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        groups[(row["task_id"], row["repeat"])][row["mode"]] = row
    complete = [group for group in groups.values()
                if set(group) == {"baseline", "auto", "oracle"}
                and all(type(item.get("success")) is bool for item in group.values())]
    rates = {}
    for mode in ("baseline", "auto", "oracle"):
        rates[mode] = (sum(group[mode]["success"] for group in complete) / len(complete)
                       if complete else None)
    by_category: dict[str, dict[str, Any]] = {}
    categories = sorted({group["auto"]["category"] for group in complete})
    for category in categories:
        subset = [group for group in complete if group["auto"]["category"] == category]
        by_category[category] = {
            "cases": len(subset),
            "success_rate": {
                mode: sum(group[mode]["success"] for group in subset) / len(subset)
                for mode in ("baseline", "auto", "oracle")
            },
            "helped": sum(group["auto"]["success"] and not group["baseline"]["success"]
                          for group in subset),
            "harmed": sum(group["baseline"]["success"] and not group["auto"]["success"]
                          for group in subset),
        }
    failures = Counter(
        row.get("failure_kind") or "none"
        for row in rows if row.get("success") is False
    )
    return {
        "complete_triples": len(complete),
        "incomplete_triples": len(groups) - len(complete),
        "success_rate": rates,
        "auto_minus_baseline": (
            rates["auto"] - rates["baseline"] if complete else None
        ),
        "oracle_minus_auto": rates["oracle"] - rates["auto"] if complete else None,
        "helped": sum(group["auto"]["success"] and not group["baseline"]["success"]
                      for group in complete),
        "harmed": sum(group["baseline"]["success"] and not group["auto"]["success"]
                      for group in complete),
        "by_category": by_category,
        "failure_kinds": dict(sorted(failures.items())),
    }
