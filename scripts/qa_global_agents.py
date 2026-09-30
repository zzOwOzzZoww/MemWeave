"""Run global Agent UI regression with isolated homes and SQLite storage."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agent_knowledge_bridge import cli, provider
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient


def main() -> None:
    output = Path(tempfile.mkdtemp(prefix='memweave-global-agents-ui-'))
    with tempfile.TemporaryDirectory(prefix='memweave-global-agents-fixture-') as fixture:
        root = Path(fixture)
        os.environ.update({
            'MEMWEAVE_HOME': str(root / 'memweave'),
            'CLAUDE_CONFIG_DIR': str(root / 'claude'),
            'CODEX_HOME': str(root / 'codex'),
            'MW_DB_PATH': str(root / 'test.db'),
            'MEMWEAVE_TEST_OUTPUT': str(output),
        })
        for key in ('MW_PROJECT_KEY', 'AKB_PROJECT_KEY', 'MW_DAEMON_TOKEN', 'MW_DAEMON_URL'):
            os.environ.pop(key, None)
        config = {'config_version': 1, 'database_path': str(root / 'test.db'),
                  'default_project': 'shared-fixture', 'runtime_port': 0}
        provider.atomic_json(root / 'memweave' / 'config.json', config)
        state = cli._start_runtime(config, cli._source_revision())
        try:
            client = MemWeaveRuntimeClient(base_url=state['url'], token=state['token'], agent_id='qa')
            for agent, adapter in (('claude-code', 'claude-hook'), ('codex', 'codex-hook'),
                                   ('custom-agent', 'runtime-api')):
                client.register_agent(agent_id=agent, display_name=agent, adapter_type=adapter)
            subprocess.run([shutil.which('node') or 'node', str(Path(__file__).with_suffix('.cjs'))],
                           check=True, timeout=60)
            print(json.dumps({'ui_artifacts': str(output)}, ensure_ascii=True))
        finally:
            cli._stop_owned_runtime(state)
            for _ in range(50):
                if not cli._runtime_healthy(state, cli._source_revision()):
                    break
                time.sleep(0.1)


if __name__ == '__main__':
    main()
