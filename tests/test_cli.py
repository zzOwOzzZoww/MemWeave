from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_knowledge_bridge import cli


class MemWeaveCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".memweave"
        self.environment = patch.dict(os.environ, {"MEMWEAVE_HOME": str(self.home)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> str:
        output = StringIO()
        with redirect_stdout(output):
            result = cli.main(list(arguments))
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_init_is_idempotent_and_status_reads_the_local_database(self) -> None:
        initialized = self.run_cli("init", "--project", "cli-test")
        self.assertIn("初始化完成", initialized)
        config_path = self.home / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["default_project"], "cli-test")
        self.assertTrue(Path(config["database_path"]).is_file())

        repeated = self.run_cli("init")
        self.assertIn("已初始化", repeated)
        status = self.run_cli("status")
        self.assertIn("当前项目: cli-test", status)
        self.assertIn("知识: 0 总计", status)
        self.assertIn("跨 Agent: 0 可共享", status)

    def test_ui_starts_runtime_and_opens_authenticated_dashboard(self) -> None:
        self.run_cli("init", "--project", "ui-test")
        runtime = {
            "pid": 123,
            "url": "http://127.0.0.1:43210",
            "token": "test-token",
            "source_revision": "revision",
        }
        with (
            patch.object(cli, "_source_revision", return_value="revision"),
            patch.object(cli, "_runtime_healthy", return_value=False),
            patch.object(cli, "_start_runtime", return_value=runtime) as start,
            patch.object(cli.webbrowser, "open") as open_browser,
        ):
            output = self.run_cli("ui")
        start.assert_called_once()
        open_browser.assert_called_once_with(
            "http://127.0.0.1:43210/knowledge#token=test-token&project=ui-test"
        )
        self.assertIn("已在后台启动", output)

    def test_runtime_revision_changes_when_packaged_source_changes(self) -> None:
        revision = cli._source_revision()
        self.assertEqual(len(revision), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in revision))


if __name__ == "__main__":
    unittest.main()
