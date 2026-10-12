"""Wake an initialized local Runtime without holding up an Agent Hook."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from . import runtime_state
from .paths import memweave_home


def recovery_configured() -> bool:
    # Explicit endpoints/local-only mode are caller boundaries, not outages
    # that authorize switching to the desktop-managed instance.
    if os.getenv('MW_RUNTIME_MODE') == 'local' or os.getenv('MW_DAEMON_URL') or os.getenv('MW_DAEMON_TOKEN'):
        return False
    try:
        config = json.loads((memweave_home() / 'config.json').read_text(encoding='utf-8-sig'))
        return (isinstance(config, dict) and config.get('config_version') == 1
                and isinstance(config.get('database_path'), str)
                and Path(config['database_path']).expanduser().resolve() == runtime_state.database_path())
    except (OSError, ValueError, TypeError):
        return False


def request_recovery() -> bool:
    if not recovery_configured():
        return False
    logs = memweave_home() / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    options = {'stdin': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    with (logs / 'runtime-recovery.log').open('ab', buffering=0) as log:
        subprocess.Popen([str(python), '-X', 'utf8', '-m', 'agent_knowledge_bridge.runtime_recovery'],
                         stdout=log, stderr=log, **options)
    return True


def main() -> int:
    if not recovery_configured():
        return 0
    # The desktop entrypoint already serializes startup, verifies ownership
    # before stopping a stale instance, and never exposes credentials.
    from .cli import main as cli_main
    return cli_main(['ui', '--no-open', '--no-setup'])


if __name__ == '__main__':
    raise SystemExit(main())
