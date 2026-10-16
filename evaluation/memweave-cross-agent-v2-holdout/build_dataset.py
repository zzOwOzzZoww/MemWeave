"""Build new synthetic fact groups, without importing the retrieval engine."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "mw-holdout-v2.0.0-draft"
CATEGORIES = ("cross_language", "direct_fact", "insufficient_detail",
              "distractor_ranking", "knowledge_update", "project_isolation")
# Values below are fictional project decisions, not recommendations for these formats.
FACTS = [
    ("font-display", "aurora-font", "font-display mode", "font-display 模式", "swap", "block"),
    ("parquet-dictionary", "vector-export", "Parquet dictionary encoding", "Parquet 字典编码", "enabled", "disabled"),
    ("xml-namespace", "prism-feed", "XML namespace prefix", "XML 命名空间前缀", "px", "legacy"),
    ("geojson-coordinate", "delta-map", "GeoJSON coordinate order", "GeoJSON 坐标顺序", "longitude-latitude", "latitude-longitude"),
    ("exif-orientation", "helix-photo", "EXIF orientation handling", "EXIF 方向处理", "normalize-before-resize", "preserve-tag"),
    ("csv-decimal", "orbit-report", "CSV decimal separator", "CSV 小数分隔符", "dot", "comma"),
    ("webvtt-timestamp", "nexus-caption", "WebVTT timestamp precision", "WebVTT 时间戳精度", "milliseconds", "seconds"),
    ("raster-nodata", "vertex-raster", "raster NoData value", "栅格 NoData 值", "-32768", "-9999"),
    ("ical-timezone", "zenith-calendar", "iCalendar timezone identifier", "iCalendar 时区标识", "Asia/Shanghai", "UTC"),
    ("svg-viewbox", "matrix-icon", "SVG viewBox", "SVG viewBox", "0 0 24 24", "0 0 32 32"),
    ("protobuf-field", "pulse-schema", "Protobuf reserved field number", "Protobuf 保留字段号", "17", "9"),
    ("avif-alpha", "echo-preview", "AVIF alpha channel handling", "AVIF alpha 通道处理", "preserve", "flatten"),
    ("zstd-seek", "quartz-archive", "Zstandard seek table interval", "Zstandard seek 表间隔", "8 MiB", "4 MiB"),
    ("bcp47-fallback", "lumen-locale", "BCP47 fallback locale", "BCP47 回退语言", "zh-Hans", "en-US"),
    ("epub-spine", "nova-reader", "EPUB spine direction", "EPUB spine 阅读方向", "left-to-right", "right-to-left"),
    ("woff2-mime", "flux-font", "WOFF2 MIME type", "WOFF2 MIME 类型", "font/woff2", "application/octet-stream"),
    ("mp4-faststart", "sigma-video", "MP4 faststart setting", "MP4 faststart 设置", "enabled", "disabled"),
    ("orc-bloom", "epsilon-table", "ORC bloom filter column", "ORC bloom filter 列", "invoice_id", "account_id"),
    ("midi-velocity", "scalar-audio", "MIDI default velocity", "MIDI 默认力度", "80", "64"),
    ("tiff-tile", "lambda-image", "TIFF tile size", "TIFF tile 大小", "256x256", "128x128"),
]


def canonical(rows):
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")


def build_cases():
    rows = []
    for index, (topic, subject, facet, zh_facet, value, old_value) in enumerate(FACTS):
        kernel = f"H2-{index + 1:03d}"
        # Interleave topics rather than putting one subject family in a split.
        split = "dev" if index % 4 == 0 else "test"
        project = f"fixture-{topic}"
        source = "codex" if index % 2 else "claude-code"
        target = "claude-code" if source == "codex" else "codex"
        en = f"{subject} uses {value} for its {facet} in the production export."
        zh = f"{subject} 的生产导出采用 {value} 作为{zh_facet}。"
        for variant, category in enumerate(CATEGORIES):
            cid = f"{kernel}-{variant + 1:02d}"
            key = cid + "-fact"

            def record(alias, content, *, status="active", relation=facet, obj=value,
                       owner=subject, scope_project=project, start="2026-09-01", end=None):
                return {"id": alias, "source_agent": source, "scope": "project",
                        "project_key": scope_project, "status": status,
                        "source_session": cid + ":" + alias,
                        "subject_terms": [owner], "content": content,
                        "fact": {"subject": owner, "relation": relation, "object": obj,
                                 "valid_from": start, "valid_to": end}}

            records = [record(key, en)]
            query = f"What is the {facet} of {subject} in the production export?"
            expected = {"decision": "inject", "evidence_ids": [key], "forbidden_ids": [],
                        "topical_ids": [key], "shadow_probe": "none", "answer": value}
            request = {"subject": subject, "relation": facet, "object_context": "production export",
                       "as_of": "2026-10-08"}
            error_kind = "none"
            if category == "cross_language":
                if index % 2:
                    records = [record(key, zh)]
                else:
                    query = f"{subject} 的生产导出采用什么{zh_facet}？"
            elif category == "insufficient_detail":
                mode = index % 3
                if mode == 0:
                    query = f"What is the rollback timeout in seconds for {subject} production export?"
                    request["relation"] = "rollback timeout"
                    error_kind = "missing_relation"
                elif mode == 1:
                    query = f"What is the {facet} of {subject} in the staging import?"
                    request["object_context"] = "staging import"
                    error_kind = "wrong_object_context"
                else:
                    query = f"What was the {facet} of {subject} before 2026-08-01?"
                    records[0]["content"] += " This decision only takes effect on 2026-09-01; no earlier setting is recorded."
                    request["as_of"] = "2026-08-01"
                    error_kind = "missing_time"
                expected.update(decision="abstain", evidence_ids=[], forbidden_ids=[key], answer="")
            elif category == "distractor_ranking":
                decoy = cid + "-decoy"
                other = subject + "-secondary"
                records.append(record(decoy, f"{other} uses {old_value} for its {facet} in the production export.",
                                      owner=other, obj=old_value))
                expected["forbidden_ids"] = [decoy]
                error_kind = "wrong_subject"
            elif category == "knowledge_update":
                old = cid + "-old"
                records = [record(old, f"{subject} previously used {old_value} for its {facet} in the production export.",
                                  status="archived", obj=old_value, start="2026-06-01", end="2026-09-01"),
                           record(key, f"{subject} now uses {value} for its {facet} in the production export, effective 2026-09-01.")]
                records[0]["superseded_by"] = key
                query = f"What is the current {facet} of {subject} in the production export?"
                expected["forbidden_ids"] = [old]
                error_kind = "superseded_value"
            elif category == "project_isolation":
                expected.update(decision="abstain", evidence_ids=[], forbidden_ids=[key], answer="", topical_ids=[])
                error_kind = "wrong_project"
            project_context = project + "-other" if category == "project_isolation" else project
            expected["answer_rubric"] = "Return only evidence supporting the requested fact; never infer an unrecorded setting."
            rows.append({"case_id": cid, "kernel_id": kernel, "split": split, "category": category,
                         "topic": topic, "source_agent": source, "target_agent": target,
                         "project_context": project_context, "query": query, "request": request,
                         "memory_records": records, "expected": expected,
                         "forbidden_error_kind": error_kind, "synthetic": True})
    return rows


def validate(rows, prior_contents=()):
    if len(rows) != 120 or Counter(r["split"] for r in rows) != {"dev": 30, "test": 90}:
        raise ValueError("Expected 120 cases split into 30 dev / 90 test")
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate case ID")
    groups = defaultdict(list)
    ids = set()
    for row in rows:
        groups[row["kernel_id"]].append(row)
        if not row["synthetic"] or row["source_agent"] == row["target_agent"]:
            raise ValueError("Non-synthetic or same-agent case")
        if not 0 < len(row["query"]) <= 500:
            raise ValueError("Query outside Core length contract")
        known = {r["id"] for r in row["memory_records"]}
        if len(known) != len(row["memory_records"]) or known & ids:
            raise ValueError("Duplicate memory ID")
        ids |= known
        exp = row["expected"]
        required, forbidden = set(exp["evidence_ids"]), set(exp["forbidden_ids"])
        if (not (required | forbidden | set(exp["topical_ids"])) <= known or required & forbidden
                or bool(required) != (exp["decision"] == "inject") or known != required | forbidden):
            raise ValueError("Invalid evidence contract")
        for record in row["memory_records"]:
            fact = record["fact"]
            if not all(fact[k] for k in ("subject", "relation", "object", "valid_from")):
                raise ValueError("Incomplete fact labels")
            if fact["valid_to"] and fact["valid_to"] <= fact["valid_from"]:
                raise ValueError("Invalid validity interval")
            if record["content"].casefold() in prior_contents:
                raise ValueError("Exact content overlap with prior benchmark")
            if record.get("superseded_by") and record["superseded_by"] not in known:
                raise ValueError("Missing replacement")
            if record["id"] in required:
                request = row["request"]
                if (fact["subject"] != request["subject"] or fact["relation"] != request["relation"]
                        or record["project_key"] != row["project_context"]
                        or fact["valid_from"] > request["as_of"]
                        or (fact["valid_to"] and request["as_of"] >= fact["valid_to"])):
                    raise ValueError("Required evidence contradicts the requested fact")
    if len(groups) != 20:
        raise ValueError("Expected 20 topic groups")
    for group in groups.values():
        if len({r["split"] for r in group}) != 1 or Counter(r["category"] for r in group) != Counter(CATEGORIES):
            raise ValueError("Topic leakage or missing scenario")
    if len({g[0]["topic"] for g in groups.values()}) != 20:
        raise ValueError("Duplicate topic")
    return {"valid": True, "cases": len(rows), "kernels": len(groups),
            "splits": dict(Counter(r["split"] for r in rows)),
            "categories": dict(Counter(r["category"] for r in rows))}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without rewriting frozen data")
    args = parser.parse_args()
    previous = ROOT.parent / "memweave-cross-agent-v1" / "cases.jsonl"
    prior = {r["content"].casefold() for line in previous.read_text(encoding="utf-8").splitlines()
             for r in json.loads(line)["memory_records"]}
    rows = build_cases()
    summary = validate(rows, prior)
    payload = canonical(rows)
    manifest = {"version": VERSION, **summary, "dataset_sha256": hashlib.sha256(payload).hexdigest(),
                "label_author": "Codex assistant; not independently adjudicated",
                "prior_dataset_sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
                "overlap_check": "No exact record content reuse; semantic topic separation is design-reviewed, not mathematically guaranteed.",
                "kernel_splits": {r["kernel_id"]: r["split"] for r in rows}}
    dataset = ROOT / "cases.jsonl"
    frozen = ROOT / "dataset-card.json"
    if args.check:
        if dataset.read_bytes() != payload or json.loads(frozen.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Frozen dataset or manifest differs from deterministic build")
    else:
        if dataset.exists() or frozen.exists():
            raise ValueError("Refuse to overwrite frozen dataset; use --check")
        dataset.write_bytes(payload)
        frozen.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
