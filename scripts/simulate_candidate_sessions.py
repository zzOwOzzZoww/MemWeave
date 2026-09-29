from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from agent_knowledge_bridge.claude_learning_adapter import (
    ClaudeLearningAdapter,
    DeepSeekReviewer,
)


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    user: str
    assistant: str
    command: str | None = None
    result: str = ""
    success: bool | None = None


CASES = (
    Case(
        "preference-language",
        "长期偏好",
        "以后解释技术问题时默认使用简体中文，命令和错误信息保留原文。",
        "已记录这个长期偏好。",
    ),
    Case(
        "project-fact-storage",
        "项目事实",
        "确认 MemWeave 使用 SQLite 和 FTS5，并把这个结论记录下来。",
        "已完成验证并记录。",
        "python -m pytest tests/test_footprint.py -q",
        "2 passed",
        True,
    ),
    Case(
        "reusable-procedure",
        "可复用流程",
        "以后修改知识生命周期后统一运行治理测试，形成可复用的项目流程。",
        "流程已验证。",
        "python -m pytest tests/test_governance.py -q",
        "18 passed",
        True,
    ),
    Case(
        "project-decision",
        "项目决策",
        "MemWeave 的新知识先进入 candidate，经过客观验证后再进入 active。",
        "决策已落地。",
        "python -m pytest tests/test_claude_learning.py -q",
        "4 passed",
        True,
    ),
    Case(
        "project-convention-no-test",
        "项目事实无测试",
        "这个项目的知识记录统一放在 data/knowledge.db，后续维护按这个约定执行。",
        "已说明项目约定。",
    ),
    Case(
        "cross-project-preference",
        "通用偏好无测试",
        "我希望不同项目都优先给出可执行步骤，再补充背景解释。",
        "已记录为长期偏好。",
    ),
    Case(
        "weather",
        "一次性问答",
        "今天北京天气怎么样？",
        "今天北京有小雨。",
    ),
    Case(
        "secret",
        "敏感信息",
        "记住我的 API Key 是 sk-simulated-secret-123456789。",
        "我不会保存凭据。",
    ),
    Case(
        "one-time-fact",
        "一次性事实",
        "刚才这个文件今天改过一次。",
        "好的。",
    ),
    Case(
        "unresolved-failure",
        "未解决失败",
        "这个方案目前失败了，之后可能继续尝试，先记下来。",
        "当前没有解决，暂不沉淀。",
        "python -m pytest tests/test_missing_case.py -q",
        "FAILED",
        False,
    ),
)


def write_transcript(path: Path, case: Case) -> None:
    entries: list[dict] = [
        {"type": "user", "message": {"role": "user", "content": case.user}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": ([
                    {
                        "type": "tool_use",
                        "id": f"tool-{case.case_id}",
                        "name": "Bash",
                        "input": {"command": case.command},
                    }
                ] if case.command else [{"type": "text", "text": case.assistant}]),
            },
        },
    ]
    if case.command:
        entries.extend(
            [
                {
                    "type": "user",
                    "toolUseResult": {"stdout": case.result, "stderr": "", "interrupted": False},
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": f"tool-{case.case_id}",
                            "is_error": case.success is False,
                            "content": case.result,
                        }],
                    },
                },
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": case.assistant}]}},
            ]
        )
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n", encoding="utf-8")


def event_ids(review_text: str) -> list[str]:
    return re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)


def deterministic_reviewer(case: Case) -> Callable[[str], dict]:
    def review(review_text: str) -> dict:
        ids = event_ids(review_text)
        if case.category in {"一次性问答", "敏感信息", "一次性事实", "未解决失败"}:
            return {"proposals": []}
        mapping = {
            "长期偏好": ("中文技术解释偏好", "默认使用简体中文解释技术问题，命令和错误信息保留原文。", "preference", "user"),
            "项目事实": ("MemWeave 存储与检索实现", "MemWeave 使用 SQLite 持久化知识，并使用 FTS5 支持全文检索。", "fact", "project"),
            "可复用流程": ("知识生命周期修改后的回归测试流程", "修改知识生命周期后运行治理测试，确认候选、晋升和过期行为没有回归。", "procedure", "project"),
            "项目决策": ("知识先候选后生效", "新知识先进入 candidate，经过客观验证后再进入 active。", "decision", "project"),
            "项目事实无测试": ("知识库文件位置约定", "项目知识记录统一保存在 data/knowledge.db。", "fact", "project"),
            "通用偏好无测试": ("回答先给可执行步骤", "跨项目回答优先给出可执行步骤，再补充必要背景解释。", "preference", "user"),
        }
        title, content, kind, scope = mapping[case.category]
        return {"proposals": [{
            "title": title,
            "content": content,
            "knowledge_type": kind,
            "scope": scope,
            "search_terms": "MemWeave knowledge memory workflow preference",
            "evidence_event_ids": ids if case.command and case.success else [],
        }]}

    return review


def read_statuses(database: Path, session_id: str) -> list[str]:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT status FROM knowledge_records WHERE source_session = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [row[0] for row in rows]


def run(mode: str) -> dict:
    rows: list[dict] = []
    with TemporaryDirectory(prefix=f"memweave-{mode}-") as temp:
        root = Path(temp)
        database = root / "simulation.db"
        for case in CASES:
            transcript = root / f"{case.case_id}.jsonl"
            write_transcript(transcript, case)
            reviewer = deterministic_reviewer(case) if mode == "mock" else DeepSeekReviewer()
            adapter = ClaudeLearningAdapter(
                database_path=database,
                agent_id="claude-code",
                project_key="candidate-simulation",
                reviewer=reviewer,
            )
            try:
                result = adapter.learn({"session_id": case.case_id, "transcript_path": str(transcript)})
                error = ""
            except Exception as exc:  # keep one bad model response from hiding other cases
                result = {"status": "error"}
                error = str(exc)
            statuses = read_statuses(database, case.case_id)
            rows.append({
                "case_id": case.case_id,
                "category": case.category,
                "proposals": int(result.get("proposals", 0)),
                "promoted": int(result.get("promoted", 0)),
                "statuses": statuses,
                "error": error,
            })
        with closing(sqlite3.connect(database)) as connection:
            status_counts = dict(connection.execute("SELECT status, COUNT(*) FROM knowledge_records GROUP BY status").fetchall())
            run_counts = connection.execute("SELECT COUNT(*), SUM(proposal_count), SUM(promoted_count) FROM learning_runs").fetchone()
    return {"mode": mode, "generated_at": datetime.now().isoformat(timespec="seconds"), "cases": rows, "status_counts": status_counts, "run_counts": {"runs": run_counts[0] or 0, "proposals": run_counts[1] or 0, "promoted": run_counts[2] or 0}}


def markdown(report: dict) -> str:
    lines = [f"# MemWeave 会话入库模拟（{report['mode']}）", "", f"生成时间：{report['generated_at']}", "", "| 会话类型 | Reviewer候选 | 自动晋升 | 最终状态 | 错误 |", "|---|---:|---:|---|---|"]
    for row in report["cases"]:
        status = ", ".join(row["statuses"]) or "未入库"
        lines.append(f"| {row['category']} | {row['proposals']} | {row['promoted']} | {status} | {row['error'] or ''} |")
    lines += ["", f"总运行：{report['run_counts']['runs']}，候选提案：{report['run_counts']['proposals']}，自动晋升：{report['run_counts']['promoted']}。", f"状态统计：{json.dumps(report['status_counts'], ensure_ascii=False)}", "", "说明：`candidate` 是进入待采纳清单，`active` 是引用了模拟的成功测试事件后自动生效；两者都表示已写入临时知识库。测试事件和命令输出是构造数据，不代表真正执行过命令，更不能证明每条结论都被对应测试覆盖。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate MemWeave candidate sessions in a temporary database")
    parser.add_argument("--mode", choices=("mock", "real", "both"), default="both")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs") / "candidate_simulation")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    modes = ("mock", "real") if args.mode == "both" else (args.mode,)
    for mode in modes:
        report = run(mode)
        (args.output_dir / f"{mode}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / f"{mode}.md").write_text(markdown(report), encoding="utf-8")
        print(markdown(report))


if __name__ == "__main__":
    main()
