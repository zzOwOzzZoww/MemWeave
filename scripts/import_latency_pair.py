"""Import one explicitly controlled Codex on/off pair from native rollout files.

No model calls, no hook reconfiguration, and no writes unless --apply is given.
Experiment conditions in the manifest are an operator attestation, not something
the native timing fields can prove (notably cache state and disabled hooks).
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_knowledge_bridge.latency_impact import record_pair, metrics
from agent_knowledge_bridge.turn_timing import read_codex_timing


def native_sample(entry, conditions):
    path = Path(entry['transcript_path']).resolve()
    session, turn = entry['session_id'], entry['turn_id']
    timing = read_codex_timing(path, session, turn)
    if timing['timing_status'] != 'captured':
        raise ValueError('native task_complete is missing/invalid; no sample imported')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('experiment rollouts must be bounded, fresh sessions (max 16 MiB)')
    current = None
    model, prompt, provider, effort = None, None, None, None
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get('payload') or {}
            if event.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                current = payload.get('turn_id')
            if event.get('type') == 'session_meta':
                provider = payload.get('model_provider')
            if event.get('type') == 'turn_context':
                current = payload.get('turn_id')
                if current == turn:
                    model, effort = payload.get('model'), payload.get('effort')
            if current == turn and event.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                message = payload.get('message')
                if isinstance(message, str):
                    prompt = message.strip()
            if (current == turn and event.get('type') == 'response_item'
                    and payload.get('type') == 'message' and payload.get('role') == 'user'):
                parts = payload.get('content') or []
                if any(part.get('type') == 'input_image' for part in parts):
                    raise ValueError('image prompts require a separate controlled protocol')
                text = '\n'.join(part.get('text', '') for part in parts
                                 if part.get('type') in {'input_text', 'text'}).strip()
                # Ignore bootstrap instructions and injected context; a controlled
                # experiment uses a single plain-text task in a fresh session.
                if text and not text.startswith(('# AGENTS.md instructions', '<environment_context>',
                                                 '<memweave_context', '<user_instructions>')):
                    prompt = text
    if not model or not prompt:
        raise ValueError('matching native model and user prompt evidence is required')
    if model != conditions['model']:
        raise ValueError('manifest model does not match the native turn')
    sample = {**conditions, 'session_id': session, 'turn_id': turn,
              'prompt_hash': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
              'timing_source': timing['timing_source'],
              'ttft_ms': timing['ttft_ms'], 'response_total_ms': timing['response_total_ms']}
    return sample, (provider, effort)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    on, on_config = native_sample(manifest['on'], manifest['conditions'])
    off, off_config = native_sample(manifest['off'], manifest['conditions'])
    if on_config != off_config:
        raise ValueError('native model provider or reasoning effort differs')
    kwargs = {k: manifest[k] for k in ('project_key', 'experiment_id', 'pair_id',
        'baseline_mode', 'execution_order', 'conditions_confirmed')}
    kwargs.update(agent_id='codex', on=on, off=off)
    # Preview also checks existing IDs and conditions against an isolated backup.
    with tempfile.TemporaryDirectory(prefix='memweave-latency-preview-') as directory:
        preview = Path(directory) / 'preview.db'
        if args.database.exists():
            with closing(sqlite3.connect(args.database)) as source, closing(sqlite3.connect(preview)) as dest:
                source.backup(dest)
        result = record_pair(preview, **kwargs)
        if args.apply:
            if not args.database.is_file():
                raise ValueError('target database does not exist')
            result = record_pair(args.database, **kwargs)
        with closing(sqlite3.connect(preview)) as db:
            db.row_factory = sqlite3.Row
            report = metrics(db, manifest['project_key'])
    print(json.dumps({'applied': args.apply, 'result': result, 'metrics': report},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
