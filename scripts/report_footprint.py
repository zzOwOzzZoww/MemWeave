"""Print the MemWeave weight budget as a markdown report.

Run this instead of quoting remembered numbers:

    python scripts/report_footprint.py

It answers three questions a prospective user actually asks before adopting a
memory layer: what does it cost to install, what does it cost to run, and does it
grow without bound. Every number is measured here, not asserted.

Notes for whoever reads the output:
- RSS figures are measured in fresh interpreters, so they include the Python
  runtime itself. Compare the *delta*, not the absolute value.
- Write to a local temp path, never a cloud-synced or antivirus-scanned folder;
  on such a path every SQLite close costs tens of milliseconds and the numbers
  stop meaning anything. See docs/MemWeave架构评审与能力提升方案_20260922.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"



#: Modules the interpreter pulls in through site .pth hooks. They are present
#: before any user code runs, so they are not a dependency of this project.
INTERPRETER_INJECTED = {"_distutils_hack", "pywin32_bootstrap"}

WORDS = (
    "ledger service retry policy backoff timeout budget connector schema "
    "checkpoint ingest pipeline tenant shard replica cache queue worker"
).split()


LAYER_PROBE = """
import ctypes, ctypes.wintypes as wt, json, os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, SRC)


class _C(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]


def rss():
    counters = _C()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_C), wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return counters.PeakWorkingSetSize / (1024 * 1024) if ok else 0.0


stage = sys.argv[1]
db = Path(tempfile.mkdtemp()) / "k.db"
base = rss()
started = time.perf_counter()
if stage != "baseline":
    if stage == "core":
        from agent_knowledge_bridge.governance import Governor
        from agent_knowledge_bridge.store import KnowledgeStore
        Governor(KnowledgeStore(db))
    elif stage == "service":
        from agent_knowledge_bridge.service import KnowledgeBridgeService
        KnowledgeBridgeService(agent_id="a", project_key="p", database_path=db)
    elif stage == "hook":
        from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
        ClaudeLearningAdapter(database_path=db, agent_id="a", project_key="p",
                              reviewer=lambda _: {"proposals": []})
    elif stage == "daemon":
        os.environ["MW_DAEMON_TOKEN"] = "t"
        from agent_knowledge_bridge.daemon import create_app
        create_app(database_path=db, api_token="t")
loaded = sorted({n.split(".")[0] for n, m in list(sys.modules.items())
                 if "site-packages" in (getattr(m, "__file__", None) or "")})
# Spelled out here rather than imported: this probe runs in a bare interpreter.
injected = {"_distutils_hack", "pywin32_bootstrap"}
print("RESULT" + json.dumps({
    "ms": round((time.perf_counter() - started) * 1000, 1),
    "rss_mb": round(rss(), 1),
    "delta_mb": round(rss() - base, 1),
    "third_party": sorted(set(loaded) - injected),
}))
"""


def layer_row(stage: str) -> dict:
    probe = PROJECT_ROOT / "_layer_probe.py"
    probe.write_text(
        LAYER_PROBE.replace("SRC", repr(str(SRC))), encoding="utf-8"
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(probe), stage],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            # User site-packages must stay visible: on this machine the web stack
            # lives there. Interpreter-injected modules are filtered by name
            # instead, which is what the count actually means.
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    finally:
        probe.unlink(missing_ok=True)
    payload = [l for l in proc.stdout.splitlines() if l.startswith("RESULT")]
    if not payload:
        return {"stage": stage, "error": proc.stderr[-300:]}
    return {"stage": stage, **json.loads(payload[-1][len("RESULT"):])}


def measure_layers() -> list[dict]:
    rows = []
    for stage, label in (
        ("baseline", "python + stdlib"),
        ("core", "core (store + governance)"),
        ("service", "service facade"),
        ("hook", "learning hook (in-process)"),
        ("daemon", "HTTP daemon"),
    ):
        result = layer_row(stage)
        rows.append({
            "stage": label,
            "ms": result.get("ms"),
            "rss_mb": result.get("rss_mb"),
            "delta_mb": result.get("delta_mb"),
            "third_party": len(result.get("third_party", [])),
            "error": result.get("error"),
        })
    return rows


def measure_storage() -> list[dict]:
    sys.path.insert(0, str(SRC))
    from agent_knowledge_bridge.store import KnowledgeStore

    root = Path(tempfile.mkdtemp())
    rows = []
    for count in (200, 1000, 5000):
        db = root / f"g{count}.db"
        store = KnowledgeStore(db)
        for index in range(count):
            store.publish(
                source_agent="probe", project_key="probe",
                title=f"svc-{index} retry policy",
                content=(
                    f"Service svc-{index} uses exponential backoff with a 300ms base delay. "
                    + " ".join(WORDS[index % len(WORDS):] + WORDS[: index % len(WORDS)])
                ),
                knowledge_type="procedure", scope="project",
                evidence_summary=f"verifier run {index} passed",
                search_terms=f"svc-{index} 重试 退避 超时",
            )
        with store._connect() as db_conn:
            db_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages = db_conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = db_conn.execute("PRAGMA page_size").fetchone()[0]
        total = pages * page_size
        rows.append({
            "records": count,
            "db_kb": round(total / 1024, 1),
            "bytes_per_record": round(total / count),
        })
    return rows


def measure_ledger_bound() -> dict:
    sys.path.insert(0, str(SRC))
    from agent_knowledge_bridge.governance import Governor
    from agent_knowledge_bridge.store import KnowledgeStore

    root = Path(tempfile.mkdtemp())
    store = KnowledgeStore(root / "ledger.db")
    ids = []
    for index in range(20):
        created = store.publish(
            source_agent="probe", project_key="probe",
            title=f"retired-{index} retry policy",
            content=f"Use exponential backoff for retired-{index} after a 503 response.",
            knowledge_type="procedure", scope="project",
            evidence_summary="verifier passed",
        )
        ids.append(created["knowledge"]["id"])
    for knowledge_id in ids:
        store.transit(knowledge_id, to_status="archived", reason="probe", actor="report")

    governor = Governor(store)
    probes = 500
    for _ in range(probes):
        governor.shadow_probe(project_key="probe", query="retired-1 retry policy")

    with store._connect() as db:
        ledger_rows = db.execute("SELECT COUNT(*) FROM shadow_hits").fetchone()[0]
        audit_rows = db.execute(
            "SELECT COUNT(*) FROM lifecycle_audit WHERE knowledge_id = ?", (ids[0],)
        ).fetchone()[0]
        max_hit_count = db.execute(
            "SELECT COALESCE(MAX(hit_count), 0) FROM shadow_hits"
        ).fetchone()[0]
    return {
        "retired_records": len(ids),
        "probes": probes,
        "ledger_rows": ledger_rows,
        "rows_if_stored_per_probe": probes * ledger_rows,
        "max_hit_count_in_one_row": max_hit_count,
        "audit_rows_for_archived_record": audit_rows,
    }


def main() -> int:
    layers = measure_layers()
    print("## Runtime footprint\n")
    print("| layer | init | peak RSS | added | third-party packages |")
    print("|---|---|---|---|---|")
    for row in layers:
        init = "—" if row["ms"] is None else f"{row['ms']} ms"
        rss = "—" if row["rss_mb"] is None else f"{row['rss_mb']} MB"
        delta = "—" if row["delta_mb"] is None else f"+{row['delta_mb']} MB"
        print(f"| {row['stage']} | {init} | {rss} | {delta} | {row['third_party']} |")

    print("\n## Storage growth\n")
    storage = measure_storage()
    print("| records | db size | bytes/record |")
    print("|---|---|---|")
    for row in storage:
        print(f"| {row['records']} | {row['db_kb']} KB | {row['bytes_per_record']} |")

    print("\n## Governance ledger bound\n")
    ledger = measure_ledger_bound()
    for key, value in ledger.items():
        print(f"- {key}: {value}")
    print(
        "\nThe ledger is one row per retired record, not one row per probe, so its "
        "size is bounded by the retirement policy rather than by query volume."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
