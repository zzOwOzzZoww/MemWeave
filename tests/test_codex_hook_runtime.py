from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex_learning_hook.py"
spec = importlib.util.spec_from_file_location("codex_learning_hook", SCRIPT)
assert spec is not None and spec.loader is not None
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

from agent_knowledge_bridge import runtime_state


class CodexHookRuntimeTest(unittest.TestCase):
    """The Codex hook must select the newest live Runtime, not the first file.

    Resolution lives in ``runtime_state`` and is shared with the Claude hook
    and the daemon, so the seam every case stubs is ``state_paths`` — the list
    of files the resolver is willing to read.  Stubbing that instead of the
    individual readers keeps these cases honest about the real ordering rule.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "data" / "runtime" / "runtime-state.json"
        self.state.parent.mkdir(parents=True)
        self.home = self.root / ".memweave"
        self.home.mkdir()
        self.global_state = self.home / "runtime-state.json"
        # Newest-first ordering is the product of these paths plus each file's
        # started_at, so the ordering is exercised rather than assumed.
        self.enter = patch.object(
            runtime_state, "state_paths", return_value=[self.global_state, self.state]
        )
        self.enter.start()
        self.addCleanup(self.enter.stop)
        self.enter_env = patch.dict(
            os.environ, {"MW_DAEMON_URL": "", "MW_DAEMON_TOKEN": "", "MW_RUNTIME_MODE": ""}
        )
        self.enter_env.start()
        self.addCleanup(self.enter_env.stop)

    def write_state(self, path: Path, url: str, **extra) -> None:
        path.write_text(
            json.dumps({"url": url, "token": "test-token", **extra}), encoding="utf-8"
        )

    def test_stale_runtime_uses_local_store(self) -> None:
        self.write_state(self.state, "http://127.0.0.1:43210")
        with patch.object(
            hook.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            self.assertIsNone(hook.selected_runtime())

    def test_global_runtime_is_used_when_project_runtime_is_stale(self) -> None:
        self.write_state(self.state, "http://127.0.0.1:43210")
        self.write_state(self.global_state, "http://127.0.0.1:43211")

        def health(request, timeout):
            self.assertEqual(timeout, 0.5)
            if ":43210/" in request.full_url:
                raise urllib.error.URLError("offline")
            return io.BytesIO(b'{"status":"ok"}')

        with patch.object(hook.urllib.request, "urlopen", side_effect=health):
            client = hook.selected_runtime()
        self.assertIsNotNone(client)
        self.assertEqual(client.base_url, "http://127.0.0.1:43211")

    def test_newer_runtime_state_wins_when_both_instances_are_healthy(self) -> None:
        self.write_state(
            self.state,
            "http://127.0.0.1:43210",
            token="old",
            started_at="2026-09-23T10:00:00+00:00",
        )
        self.write_state(
            self.global_state,
            "http://127.0.0.1:43211",
            token="new",
            started_at="2026-09-23T11:00:00+00:00",
        )
        with patch.object(
            hook.urllib.request, "urlopen", return_value=io.BytesIO(b'{"status":"ok"}')
        ):
            client = hook.selected_runtime()
        self.assertIsNotNone(client)
        self.assertEqual(client.base_url, "http://127.0.0.1:43211")

    def test_recency_beats_state_file_order(self) -> None:
        """A stale project file must lose to a newer global one.

        The old reader returned the first state file that parsed, so a leftover
        from an earlier launcher shadowed the live Runtime and every recall went
        to a dead port.
        """
        self.write_state(
            self.state,
            "http://127.0.0.1:43210",
            started_at="2026-09-24T12:00:00+00:00",
        )
        self.write_state(
            self.global_state,
            "http://127.0.0.1:43211",
            started_at="2026-09-23T11:00:00+00:00",
        )
        with patch.object(
            hook.urllib.request, "urlopen", return_value=io.BytesIO(b'{"status":"ok"}')
        ):
            client = hook.selected_runtime()
        self.assertIsNotNone(client)
        self.assertEqual(client.base_url, "http://127.0.0.1:43210")

    def test_legacy_database_key_is_honoured(self) -> None:
        """A state file spelling the path 'database' must not be ignored."""
        legacy = self.root / "legacy.db"
        self.global_state.write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:43211",
                    "token": "t",
                    "database": str(legacy),
                    "started_at": "2026-09-24T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(hook.selected_database(), legacy.resolve())

    def test_both_hooks_resolve_the_same_database(self) -> None:
        """The two agents must never be pointed at different databases."""
        shared = self.root / "shared.db"
        self.global_state.write_text(
            json.dumps({"database_path": str(shared), "started_at": "2026-09-24T12:00:00+00:00"}),
            encoding="utf-8",
        )
        claude = importlib.util.spec_from_file_location(
            "claude_learning_hook", ROOT / "scripts" / "claude_learning_hook.py"
        )
        assert claude is not None and claude.loader is not None
        module = importlib.util.module_from_spec(claude)
        claude.loader.exec_module(module)
        self.assertEqual(module.selected_database(), hook.selected_database())
        self.assertEqual(module.selected_database(), shared.resolve())

    def test_daemon_default_follows_recorded_config(self) -> None:
        """The daemon's default must come from recorded config, not location.

        ``default_database_path`` deliberately does not read the Runtime state
        files the hooks scan: the daemon *creates* that state, so reading it
        would be circular.  What the two sides must agree on is the recorded
        ``config.json`` that ``memweave init`` writes, which is what this pins.
        """
        from agent_knowledge_bridge import paths

        configured = self.root / "configured.db"
        fake_home = self.root / "fake-home"
        fake_home.mkdir()
        (fake_home / "config.json").write_text(
            json.dumps({"config_version": 1, "database_path": str(configured)}),
            encoding="utf-8",
        )
        with patch.object(paths, "memweave_home", return_value=fake_home):
            resolved = paths.default_database_path(repo_root=self.root / "empty-checkout")
        self.assertEqual(resolved, configured.resolve())

    def test_explicit_env_beats_every_recorded_value(self) -> None:
        """MW_DB_PATH must win outright, in the daemon and in the hooks."""
        from agent_knowledge_bridge import paths

        explicit = self.root / "explicit.db"
        self.global_state.write_text(
            json.dumps({"database_path": str(self.root / "recorded.db")}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"MW_DB_PATH": str(explicit)}):
            self.assertEqual(hook.selected_database(), explicit.resolve())
            self.assertEqual(paths.default_database_path(repo_root=self.root), explicit.resolve())


if __name__ == "__main__":
    unittest.main()
