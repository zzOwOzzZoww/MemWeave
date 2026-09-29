"""100 deterministic experience-loop checks with real local verifier processes.

The extractor and consumer are scripted fixtures, NOT language models.
This measures plumbing, applicability and outcome accounting, not agent learning
quality or causal/generalized task improvement.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.claude_transcript import ToolEvent, TranscriptTurn
from agent_knowledge_bridge.experiences import metrics as experience_metrics

COMMAND = "python verify_widget.py"
CHECKER = """import json,sys
from pathlib import Path
actual=json.loads(Path('widget.json').read_text(encoding='utf-8'))
expected=json.loads(Path('expected.json').read_text(encoding='utf-8'))
ok=list(actual)==expected
print('PASS' if ok else 'FAIL: field order')
sys.exit(0 if ok else 1)
"""


def run_check(workspace, index):
    result = subprocess.run([sys.executable, "verify_widget.py"], cwd=workspace,
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return ToolEvent(f"check-{index}", "exec_command", json.dumps({"cmd": COMMAND}),
                     result.stdout.strip(), result.returncode == 0, "test")


def write_widget(workspace, keys):
    (workspace / "widget.json").write_text(json.dumps(dict.fromkeys(keys, 1)), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "experience-loop-20260924")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = []
    started = time.perf_counter()
    baseline_failures = transfer_passes = 0
    with tempfile.TemporaryDirectory(prefix="memweave-experience-") as folder:
        root = Path(folder)
        database = root / "test.db"
        for index in range(20):
            workspace = root / str(index)
            workspace.mkdir()
            keys = [f"header_{index}", f"payload_{index}", f"footer_{index}"]
            (workspace / "expected.json").write_text(json.dumps(keys), encoding="utf-8")
            (workspace / "verify_widget.py").write_text(CHECKER, encoding="utf-8")
            topic = f"widget-case-{index:02d}"
            contract = {
                "version": 1, "applies_when": [topic, "Windows"], "exclude_when": ["Linux"],
                "steps": ["Preserve the verified field order.", json.dumps({"ordered_keys": keys})],
                "avoid": ["Do not reverse the fields."],
                "reason": "Reversed fields failed the verifier; the expected ordering passed.",
                "verifier": {"kind": "observed_command", "command": COMMAND}}
            source, target = ("claude-code", "codex") if index % 2 == 0 else ("codex", "claude-code")
            def adapter(agent, project="experiment", reviewer=None):
                cls = ClaudeLearningAdapter if agent == "claude-code" else CodexLearningAdapter
                return cls(database_path=database, agent_id=agent, project_key=project,
                           reviewer=reviewer or (lambda _: {"proposals": []}))
            def reviewer(text):
                return {"proposals": [{"title": topic + " field order",
                    "content": "Preserve verified field order.", "knowledge_type": "procedure",
                    "experience": copy.deepcopy(contract),
                    "evidence_event_ids": re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", text)}]}
            write_widget(workspace, list(reversed(keys)))
            failure = run_check(workspace, "source-failure")
            write_widget(workspace, keys)
            success = run_check(workspace, "source-success")
            turn = TranscriptTurn(f"Build {topic} on Windows; Linux is excluded.",
                                  "Corrected field order.", (failure, success))
            learner = adapter(source, reviewer=reviewer)
            learner.transcript_parser = lambda *a, **k: turn
            learned = learner.learn({"session_id": f"source-{index}", "transcript_path": "fixture"})
            assert learned["promoted"] == 1
            consumer = adapter(target)
            prompt = f"Build {topic} on Windows"
            # Same fixed scripted consumer baseline: no memory, reversed output.
            write_widget(workspace, list(reversed(keys)))
            baseline = run_check(workspace, "baseline")
            baseline_failures += int(baseline.success is False)
            result = consumer.recall({"session_id": f"target-{index}", "turn_id": "transfer", "prompt": prompt})
            context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
            # Scripted consumer reads the instruction actually injected.
            match = re.search(r'\{"ordered_keys": \[[^\n]+\]\}', context)
            if match:
                write_widget(workspace, json.loads(match.group(0))["ordered_keys"])
            observed = run_check(workspace, "transfer")
            transfer_passes += int(observed.success is True)
            consumer.reuse.complete(agent_id=target, project_key="experiment", session_id=f"target-{index}",
                turn_id="transfer", turn=TranscriptTurn(prompt, "Checked output", (observed,)))
            cases.append({"case": f"{index}-transfer", "pass": bool(match) and observed.success is True,
                          "direction": source + "->" + target, "real_verifier": True})

            excluded = consumer.recall({"session_id": f"negative-{index}", "turn_id": "excluded",
                                        "prompt": prompt + " Linux"})
            cases.append({"case": f"{index}-exclude", "pass": not excluded})
            foreign = adapter(target, project="other").recall(
                {"session_id": f"foreign-{index}", "turn_id": "foreign", "prompt": prompt})
            cases.append({"case": f"{index}-project", "pass": not foreign})

            consumer.recall({"session_id": f"unchecked-{index}", "turn_id": "unchecked", "prompt": prompt})
            consumer.reuse.complete(agent_id=target, project_key="experiment", session_id=f"unchecked-{index}",
                turn_id="unchecked", turn=TranscriptTurn(prompt, "No verifier executed", ()))
            item = json.loads(consumer.reuse.existing(target, "experiment", f"unchecked-{index}", "unchecked")["items_json"])[0]
            cases.append({"case": f"{index}-unchecked", "pass": item["experience_result"]["status"] == "not_checked"})

            consumer.recall({"session_id": f"counter-{index}", "turn_id": "counter", "prompt": prompt})
            write_widget(workspace, list(reversed(keys)))
            counter = run_check(workspace, "counter")
            consumer.reuse.complete(agent_id=target, project_key="experiment", session_id=f"counter-{index}",
                turn_id="counter", turn=TranscriptTurn(prompt, "Validation failed", (counter,)))
            after = consumer.recall({"session_id": f"after-{index}", "turn_id": "after", "prompt": prompt})
            cases.append({"case": f"{index}-suspend", "pass": not after and counter.success is False})
        final_counts = experience_metrics(consumer.store.knowledge, "experiment")
    report = {
        "measurement": "Deterministic integration fixtures; scripted extractor and consumer, real local verifier subprocesses. No LLM, no evidence of general agent improvement.",
        "cases": len(cases), "passed": sum(c["pass"] for c in cases), "failed": sum(not c["pass"] for c in cases),
        "baseline_scripted_failures": baseline_failures, "scripted_transfer_passes": transfer_passes,
        "source_pairs": 20, "directions": {"claude-code->codex": 10, "codex->claude-code": 10},
        "real_verifier_processes": 100, "final_counts": final_counts,
        "elapsed_seconds": round(time.perf_counter() - started, 3), "results": cases}
    (args.output / "integration-100.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    return int(report["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())

