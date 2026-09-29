"""Offline diagnostics; never calls a model or opens the user's live database."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.evaluation import retrieval_metrics, paired_metrics
from agent_knowledge_bridge.reuse import ReuseStore, digest


def publish_fixture(adapter, title, content, source="claude-code"):
    knowledge = adapter.store.knowledge.publish(source_agent=source,
        project_key="memweave-evaluation", title=title, content=content, knowledge_type="procedure",
        evidence_summary="Synthetic evaluation fixture; not production learning", scope="project")
    key = knowledge["knowledge"]["id"]
    adapter.store.knowledge.feedback(agent_id="evaluation-fixture", knowledge_id=key,
        outcome="verified", evidence_kind="test", evidence_ref="fixture:explicit-test-setup",
        evidence_summary="Active fixture for retrieval evaluation; not evidence of model adoption")
    return key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--paired-results", type=Path, help="Optional real baseline/auto/oracle JSONL")
    args = parser.parse_args()
    out = args.output or ROOT / "outputs" / ("evaluation_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=False)
    database = out / "evaluation.db"
    adapter = ClaudeLearningAdapter(database_path=database, agent_id="codex",
        project_key="memweave-evaluation", reviewer=lambda _: {"proposals": []})
    dataset_bytes = (ROOT / "evaluation" / "retrieval_cases.json").read_text(encoding="utf-8")
    dataset = json.loads(dataset_bytes)
    ids = {item["key"]: publish_fixture(adapter, item["title"], item["content"]) for item in dataset["knowledge"]}
    cases = []
    for case in dataset["cases"]:
        start = time.perf_counter()
        adapter.recall({"session_id":"retrieval", "turn_id":case["id"], "prompt":case["query"]})
        elapsed = (time.perf_counter() - start) * 1000
        trace = adapter.reuse.list("memweave-evaluation", limit=1)[0]
        cases.append({**case, "expected_ids":[ids[k] for k in case["expected"]],
            "retrieved_ids":[i["knowledge_id"] for i in trace["items"]],
            "emitted_ids":[i["knowledge_id"] for i in trace["items"] if i["emitted"]],
            "retrieval_ms":trace["retrieval_ms"], "adapter_ms":elapsed, "trace_id":trace["id"]})

    # Two directions through the actual adapter + verifier; a deterministic fixture
    # writes artifacts. This does NOT claim a live Claude/Codex model used memory.
    evidence_demo = []
    for source, consumer, keyword in (("claude-code","codex","demoalpha"), ("codex","claude-code","demobeta")):
        filename = keyword + ".json"
        content = json.dumps({"instruction":"Write widget version 7 to " + filename,
            "reuse_check":{"kind":"json_equals", "artifact":filename, "equals":{"version":7}}})
        kid = publish_fixture(adapter, keyword, content, source)
        receiver = ClaudeLearningAdapter(database_path=database, agent_id=consumer,
            project_key="memweave-evaluation", reviewer=lambda _: {"proposals": []})
        receiver.recall({"session_id":"deterministic-demo", "turn_id":keyword,
                        "prompt":keyword, "cwd":str(out.resolve())})
        trace = receiver.reuse.list("memweave-evaluation", kid, 1)[0]
        (out / filename).write_text('{"version":7}', encoding="utf-8")
        receiver.transcript_parser = lambda *_, **__: TranscriptTurn(keyword, trace["id"] + " " + kid, ())
        receiver.learn({"session_id":"deterministic-demo", "turn_id":keyword, "transcript_path":""})
        evidence_demo.extend(receiver.reuse.list("memweave-evaluation", kid, 1))

    paired = None
    if args.paired_results:
        rows = [json.loads(line) for line in args.paired_results.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        paired = paired_metrics(rows)
    metrics = retrieval_metrics(cases)
    report = {"kind":"offline_diagnostic_not_live_agent_evaluation", "dataset_version":dataset["version"],
        "dataset_sha256":digest(dataset_bytes), "knowledge_snapshot":ids,
        "retrieval":metrics, "cases":cases, "deterministic_evidence_demo":evidence_demo,
        "paired_live_outcomes":paired,
        "limitations":["Small hand-labelled dataset", "Warm process, sequential queries; no concurrent load",
                        "No model call; TTFT and causal task improvement unmeasured",
                        "Constraint verification is limited to opt-in JSON equality checks"]}
    (out / "report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def percent(value):
        return "未测量" if value is None else f"{value:.1%}"
    text = f"""# MemWeave 本地诊断结果

这是小型离线诊断，不是真实 Claude Code / Codex 模型效果评测。未调用付费 API。

| 指标 | 结果 |
|---|---:|
| 样本数 | {metrics['cases']}（正样本 {metrics['positive_cases']}，负样本 {metrics['negative_cases']}） |
| Precision@3 | {percent(metrics['precision_at_k'])} |
| Recall@3 | {percent(metrics['recall_at_k'])} |
| MRR@3 | {metrics['mrr_at_k']:.3f} |
| 无关请求注入率 | {percent(metrics['negative_injection_rate'])} |
| 检索 P50 / P95 | {metrics['retrieval_p50_ms']} / {metrics['retrieval_p95_ms']} ms |
| Adapter P50 / P95 | {metrics['adapter_p50_ms']} / {metrics['adapter_p95_ms']} ms |
| 真实首 token 延迟 | 未测量 |
| 真实任务成功率提升 | {'见 report.json 的 paired_live_outcomes' if paired else '未测量'} |

Precision@3 分母固定为 3；不足 3 条仍除以 3。Recall/MRR 仅对有相关答案的查询计算。
负样本单独计算“本不该注入却注入”的比例；零召回不伪装成召回正确。
10 条改写查询使用中文、知识使用英文，用于暴露当前 FTS 检索的语义和跨语言边界。

双向确定性演示已保存至 evaluation.db：两个方向都经过 Adapter 的 recall/learn 与独立产物检查。
产物由测试脚本写入，只证明证据链和验证器工作，不能证明真实模型更聪明。
逐条预期 ID、实际 ID、耗时及 trace_id 见 report.json，可定位漏召回和错误注入。

测量包含进程内检索与 Adapter 持久化开销，不含 Hook 进程启动、HTTP、模型生成和首 token 时间。
"""
    (out / "结果说明.md").write_text(text,encoding="utf-8")
    print(json.dumps({"output":str(out.resolve()),"metrics":metrics},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
