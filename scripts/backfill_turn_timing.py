"""Backfill existing traces using native timing, without re-learning conversations."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_knowledge_bridge.turn_timing import capture_codex_timing, read_codex_timing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--audit-log', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Require an existing migrated database; this command never creates one.
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute("""SELECT session_id,turn_id FROM reuse_traces
            WHERE project_key=? AND agent_id='codex' AND turn_id IS NOT NULL
              AND timing_status NOT IN ('captured','partial') ORDER BY created_at DESC LIMIT 200""",
            (args.project,)).fetchall()
    wanted = set(rows)
    paths = {}
    # Operational audit metadata only. Avoid scanning all local Agent sessions.
    with args.audit_log.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 4194304))
        if size > 4194304:
            stream.readline()
        for line in stream.read(4194304).splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            key = (record.get('session_id'), record.get('turn_id'))
            if key in wanted and record.get('transcript_path'):
                paths[key] = record['transcript_path']
    counts = Counter()
    for session, turn in rows:
        path = paths.get((session, turn))
        if not path:
            counts['no_audit_path'] += 1
            continue
        value = read_codex_timing(path, session, turn)
        status = value['timing_status']
        if args.apply and status in {'captured','partial','aborted'}:
            capture_codex_timing(args.database, project_key=args.project, session_id=session,
                                 turn_id=turn, transcript_path=path)
        counts[status] += 1
    report = {'applied':args.apply,'existing_traces_examined':len(rows),'results':dict(counts),
              'raw_conversations_stored':False,'model_calls':0,'new_traces_created':0,
              'scope':'200 recent unresolved Codex traces; 4 MiB audit tail; 512 KiB per transcript tail'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
