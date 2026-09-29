from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


DEMO_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = DEMO_ROOT / "data" / "knowledge.db"
HANDOFF_PATH = DEMO_ROOT / "codex-workspace" / "handoff-result.json"
ACCEPTANCE_PATH = DEMO_ROOT / "claude-workspace" / "acceptance-result.json"
DEMO_ID = "MW-DEMO-HANDOFF-20260918"
RECEIPT_MARKER = "MW-DEMO-RECEIPT-20260918"
EXPECTED_POLICY = {
    "demo_id": DEMO_ID,
    "policy_name": "evidence-first",
    "max_candidates": 5,
    "archive_ttl_days": 14,
    "promote_on": ["user_approved", "test_verified"],
    "default_search_scope": "active_only",
    "archived_recall": "explicit_user_intent",
}


class VerificationError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise VerificationError(f"缺少文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"无法解析 JSON：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise VerificationError(f"JSON 顶层必须是对象：{path}")
    return data


def connect() -> sqlite3.Connection:
    if not DATABASE_PATH.exists():
        raise VerificationError(
            f"Demo 数据库不存在：{DATABASE_PATH}。请先运行 reset 并完成发布阶段。"
        )
    connection = sqlite3.connect(
        f"file:{DATABASE_PATH.as_posix()}?mode=ro", uri=True, timeout=5.0
    )
    connection.row_factory = sqlite3.Row
    return connection


def find_record(marker: str, source_agent: str) -> sqlite3.Row:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM knowledge_records
            WHERE source_agent = ? AND (title LIKE ? OR content LIKE ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_agent, f"%{marker}%", f"%{marker}%"),
        ).fetchone()
    if row is None:
        raise VerificationError(
            f"没有找到 source_agent={source_agent}、marker={marker} 的知识记录"
        )
    return row


def feedback_exists(knowledge_id: str, agent_id: str, outcome: str) -> bool:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM knowledge_evidence
            WHERE knowledge_id = ? AND agent_id = ? AND outcome = ?
            LIMIT 1
            """,
            (knowledge_id, agent_id, outcome),
        ).fetchone()
    return row is not None


def extract_policy(content: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("demo_id") == DEMO_ID:
            return candidate
    raise VerificationError("Claude Code published record has no parseable policy JSON")


def verify_handoff() -> dict[str, Any]:
    data = load_json(HANDOFF_PATH)
    source = find_record(DEMO_ID, "claude-code")
    source_policy = extract_policy(source["content"])
    for key, expected in EXPECTED_POLICY.items():
        if source_policy.get(key) != expected:
            raise VerificationError(
                f"Claude Code published policy field {key!r} does not match the authority"
            )
        if data.get(key) != expected:
            raise VerificationError(
                f"handoff-result.json 字段 {key!r} 不匹配："
                f"期望 {expected!r}，实际 {data.get(key)!r}"
            )
    required = {
        "source_agent": "claude-code",
        "cross_agent": True,
        "generated_by": "codex",
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            raise VerificationError(
                f"handoff-result.json 字段 {key!r} 必须为 {expected!r}"
            )

    if data.get("source_knowledge_id") != source["id"]:
        raise VerificationError(
            "handoff-result.json 的 source_knowledge_id 与 Claude Code 发布记录不一致"
        )
    if source["status"] not in {"candidate", "active"}:
        raise VerificationError("Claude Code 策略知识既不是 candidate 也不是 active")
    return {
        "stage": "handoff",
        "source_knowledge_id": source["id"],
        "artifact": str(HANDOFF_PATH),
    }


def verify_acceptance() -> dict[str, Any]:
    handoff = verify_handoff()
    receipt = find_record(RECEIPT_MARKER, "codex")
    if receipt["status"] not in {"candidate", "active"}:
        raise VerificationError("Codex 回执既不是 candidate 也不是 active")
    receipt_content = receipt["content"]
    for token in (
        DEMO_ID,
        handoff["source_knowledge_id"],
        "validator=PASS",
        "output=handoff-result.json",
    ):
        if token not in receipt_content:
            raise VerificationError(f"Codex 回执正文缺少：{token}")

    data = load_json(ACCEPTANCE_PATH)
    required = {
        "demo_id": DEMO_ID,
        "receipt_marker": RECEIPT_MARKER,
        "receipt_knowledge_id": receipt["id"],
        "source_agent": "codex",
        "cross_agent": True,
        "accepted_by": "claude-code",
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            raise VerificationError(
                f"acceptance-result.json 字段 {key!r} 不匹配："
                f"期望 {expected!r}，实际 {data.get(key)!r}"
            )
    return {
        "stage": "acceptance",
        "source_knowledge_id": handoff["source_knowledge_id"],
        "receipt_knowledge_id": receipt["id"],
        "artifact": str(ACCEPTANCE_PATH),
    }


def verify_final() -> dict[str, Any]:
    result = verify_acceptance()
    if not feedback_exists(result["source_knowledge_id"], "codex", "verified"):
        raise VerificationError("Claude Code 策略缺少 Codex 的 verified 证据")
    if not feedback_exists(
        result["receipt_knowledge_id"], "claude-code", "verified"
    ):
        raise VerificationError("Codex 回执缺少 Claude Code 的 verified 证据")
    source = find_record(DEMO_ID, "claude-code")
    receipt = find_record(RECEIPT_MARKER, "codex")
    if source["status"] != "active" or receipt["status"] != "active":
        raise VerificationError("双向知识没有全部从 candidate 晋升为 active")
    result["stage"] = "final"
    result["bidirectional_evidence"] = True
    return result


def reset() -> None:
    targets = [
        DATABASE_PATH,
        DATABASE_PATH.with_name(DATABASE_PATH.name + "-shm"),
        DATABASE_PATH.with_name(DATABASE_PATH.name + "-wal"),
        HANDOFF_PATH,
        ACCEPTANCE_PATH,
    ]
    for target in targets:
        if target.exists():
            target.unlink()
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("RESET PASS")
    print(f"Demo database: {DATABASE_PATH}")


def status() -> None:
    if not DATABASE_PATH.exists():
        print("No Demo database. Run reset and the publish stage first.")
        return
    with connect() as connection:
        records = connection.execute(
            """
            SELECT id, title, source_agent, status, adopted_count,
                   verified_count, rejected_count, created_at
            FROM knowledge_records
            ORDER BY created_at ASC
            """
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT knowledge_id, agent_id, outcome, summary, created_at
            FROM knowledge_evidence
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()

    print(f"Database: {json.dumps(str(DATABASE_PATH), ensure_ascii=True)}")
    print(f"Knowledge records: {len(records)}")
    for row in records:
        title = json.dumps(row["title"], ensure_ascii=True)
        print(
            f"- {row['id']} | source={row['source_agent']} | "
            f"status={row['status']} | verified={row['verified_count']} | "
            f"title={title}"
        )
        for item in evidence:
            if item["knowledge_id"] == row["id"]:
                summary = json.dumps(item["summary"], ensure_ascii=True)
                print(
                    f"    {item['outcome']} by {item['agent_id']}: "
                    f"{summary}"
                )
    print(f"Codex artifact: {'present' if HANDOFF_PATH.exists() else 'missing'}")
    print(f"Claude artifact: {'present' if ACCEPTANCE_PATH.exists() else 'missing'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="控制和验收 MemWeave Demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("reset", help="清理独立数据库和 Demo 输出")
    verify_parser = subparsers.add_parser("verify", help="运行客观验证")
    verify_parser.add_argument(
        "--stage",
        choices=("handoff", "acceptance", "final"),
        default="final",
    )
    subparsers.add_parser("status", help="查看知识记录和证据链")
    args = parser.parse_args()

    try:
        if args.command == "reset":
            reset()
        elif args.command == "status":
            status()
        else:
            if args.stage == "handoff":
                result = verify_handoff()
            elif args.stage == "acceptance":
                result = verify_acceptance()
            else:
                result = verify_final()
            print(f"VERIFY {args.stage.upper()} PASS")
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except VerificationError as exc:
        print(f"VERIFY FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
