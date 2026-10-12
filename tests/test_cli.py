from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_runtime_restart_preserves_dashboard_credentials_for_same_instance(self) -> None:
        self.run_cli('init')
        config = cli._load_config()
        old = {'url': 'http://127.0.0.1:8765', 'token': 'existing-dashboard-token',
               'database_path': config['database_path']}
        cli._write_json(cli._paths()['state'], old)
        process = Mock(pid=123)
        process.poll.return_value = None
        with (
            patch.object(cli, '_free_port', return_value=8765),
            patch.object(cli.subprocess, 'Popen', return_value=process) as start,
            patch.object(cli, '_runtime_healthy', return_value=True),
            patch.object(cli, '_request_json', return_value={'pid': 123}),
        ):
            restarted = cli._start_runtime(config, 'new-revision')
        self.assertEqual(restarted['token'], old['token'])
        from fastapi.testclient import TestClient
        from agent_knowledge_bridge.daemon import create_app
        app = create_app(database_path=Path(config['database_path']),
                         api_token=start.call_args.kwargs['env']['MW_DAEMON_TOKEN'])
        with TestClient(app) as client:
            self.assertEqual(client.get('/v1/health', headers={
                'Authorization': 'Bearer ' + old['token']}).status_code, 200)
            self.assertEqual(client.get('/v1/health', headers={
                'Authorization': 'Bearer wrong-token'}).status_code, 401)

    def test_runtime_restart_does_not_reuse_credentials_across_instance_boundaries(self) -> None:
        self.run_cli('init')
        config = cli._load_config()
        base = {'url': 'http://127.0.0.1:8765', 'token': 'old-token',
                'database_path': config['database_path']}
        for changed in ({'database_path': str(self.home / 'other.db')},
                        {'url': 'http://127.0.0.1:8766'}, {'token': ''},
                        {'database_path': None}, {'token': ['invalid']}):
            with self.subTest(changed=changed):
                cli._write_json(cli._paths()['state'], {**base, **changed})
                process = Mock(pid=123)
                process.poll.return_value = None
                with (
                    patch.object(cli, '_free_port', return_value=8765),
                    patch.object(cli.subprocess, 'Popen', return_value=process),
                    patch.object(cli, '_runtime_healthy', return_value=True),
                    patch.object(cli, '_request_json', return_value={'pid': 123}),
                ):
                    restarted = cli._start_runtime(config, 'new-revision')
                self.assertNotEqual(restarted['token'], base['token'])
                self.assertEqual(len(restarted['token']), 48)

    def test_real_runtime_restart_keeps_open_dashboard_authenticated(self) -> None:
        self.run_cli('init')
        config = cli._load_config()
        config['runtime_port'] = cli._free_port(0)
        runtime = None
        try:
            runtime = cli._start_runtime(config, 'before-update')
            old_url, old_token = runtime['url'], runtime['token']
            cli._stop_owned_runtime(runtime)
            runtime = cli._start_runtime(config, 'after-update')
            self.assertTrue(runtime['url'] == old_url)
            self.assertTrue(runtime['token'] == old_token)
            self.assertIsNotNone(cli._request_json(old_url + '/v1/health', old_token))
            self.assertIsNone(cli._request_json(old_url + '/v1/health', 'invalid-token'))
        finally:
            cli._stop_owned_runtime(runtime)


if __name__ == "__main__":
    unittest.main()
