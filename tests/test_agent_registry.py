from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from agent_knowledge_bridge.agent_registry import discover_agents, hook_configuration, install_native_hook
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.store import KnowledgeStore


class AgentRegistryTest(unittest.TestCase):
    def setUp(self):
        self.isolated_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.isolated_home.cleanup)
        self.home_patch = patch.dict(os.environ, {'MEMWEAVE_HOME': self.isolated_home.name})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        from agent_knowledge_bridge import agent_registry
        original = agent_registry._expand
        self.path_patch = patch.object(agent_registry, '_expand',
            side_effect=lambda path: Path(self.isolated_home.name) / path[2:] if path.startswith('~/') else original(path))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_discovery_is_non_invasive_and_has_stable_result_shape(self) -> None:
        results = discover_agents()
        self.assertGreaterEqual(len(results), 2)
        by_id = {item["agent_id"]: item for item in results}
        self.assertIn("claude-code", by_id)
        self.assertIn("codex", by_id)
        self.assertIn("qoder", by_id)
        self.assertIn("workbuddy", by_id)
        for item in results:
            self.assertIn("installed", item)
            self.assertIn("detected_by", item)
            self.assertIn("capabilities", item)
            self.assertIsInstance(item["detected_by"], list)
            self.assertIsInstance(item["capabilities"], list)

    def test_store_registration_is_idempotent_and_soft_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "agents.db")
            first = store.register_agent(
                agent_id="local-reviewer",
                display_name="Local Reviewer",
                adapter_type="custom",
                installed=True,
                detected_by=["config"],
                capabilities=["shared-knowledge"],
            )
            second = store.register_agent(
                agent_id="local-reviewer",
                display_name="Local Reviewer v2",
                adapter_type="runtime-api",
                installed=False,
                detected_by=[],
            )
            self.assertEqual(first["agent_id"], second["agent_id"])
            self.assertEqual(second["display_name"], "Local Reviewer v2")
            self.assertEqual(len(store.list_agents()), 1)
            disabled = store.disable_agent("local-reviewer")
            self.assertFalse(disabled["enabled"])
            self.assertEqual(store.list_agents(), [])
            self.assertEqual(len(store.list_agents(include_disabled=True)), 1)

    def test_hook_configuration_is_explicit_and_read_only(self) -> None:
        claude = hook_configuration("claude-code")
        codex = hook_configuration("codex")
        self.assertTrue(claude["supported"])
        self.assertTrue(codex["supported"])
        self.assertIn("configured", claude)
        self.assertIn("matched_paths", codex)

    def test_disable_and_rejoin_preserve_learning_and_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            learning = LearningStore(Path(temp) / 'agents.db')
            store = learning.knowledge
            agent = dict(agent_id='workbuddy', display_name='WorkBuddy', adapter_type='runtime-api')
            store.register_agent(**agent)
            run_id = learning.begin_run(agent_id='workbuddy', project_key='test', session_id='fixture',
                                        turn_hash='fixture', input_chars=10)
            key = store.publish(source_agent='workbuddy', project_key='test', title='Historical fixture',
                content='Synthetic knowledge survives disable.', scope='project', knowledge_type='fact',
                evidence_summary='Synthetic observation')['knowledge']['id']
            learning.link_compilation(run_id, key, 'produced')
            learning.finish_run(run_id, status='completed', proposal_count=1)
            store.disable_agent('workbuddy')
            assert store.list_agents() == []
            assert store.list_agents(include_disabled=True)[0]['enabled'] is False
            assert store.get(requester_agent='human-review', knowledge_id=key)['knowledge']['source_agent'] == 'workbuddy'
            assert store.latest_learning('workbuddy')['run_id'] == run_id
            assert store.register_agent(**agent)['enabled'] is True
            assert len(store.list_agents()) == 1
            assert store.latest_learning('workbuddy')['pending_count'] == 1

    def test_native_hook_install_is_additive_idempotent_and_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "settings.json"
            config.write_text('{"env":{"KEEP":"yes"},"hooks":{"Stop":[{"matcher":"","hooks":[]}]}}', encoding="utf-8")
            first = install_native_hook("claude-code", config_path=config)
            installed = json.loads(config.read_text(encoding="utf-8"))
            self.assertTrue(first["changed"])
            self.assertEqual(installed["env"]["KEEP"], "yes")
            self.assertIn("claude_learning_hook.py", json.dumps(installed))
            self.assertTrue(Path(first["backup_path"]).is_file())
            second = install_native_hook("claude-code", config_path=config)
            self.assertFalse(second["changed"])
            self.assertEqual(json.dumps(installed, sort_keys=True), json.dumps(json.loads(config.read_text(encoding="utf-8")), sort_keys=True))

    def test_installed_hook_is_never_wrapped_in_a_shell(self) -> None:
        """A ``shell`` field silently killed every hook invocation.

        The client wrapped the command as ``powershell -Command "<command>"``,
        which re-tokenised the quoted interpreter as one string and the trailing
        bare ``hook`` as a second command, so the hook exited with
        ``UnexpectedToken`` before Python started.  Neither a fresh install nor
        a repair pass may reintroduce the wrapper.
        """
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "settings.json"
            first = install_native_hook("claude-code", config_path=config)
            self.assertTrue(first["changed"])
            items = [
                item
                for entry in json.loads(config.read_text(encoding="utf-8"))["hooks"].values()
                for entry_hooks in [entry]
                for item in entry_hooks[0]["hooks"]
            ]
            self.assertTrue(items)
            for item in items:
                self.assertNotIn("shell", item)
                self.assertIs(item["async"], False)

            # An install left behind by an older version, complete with the
            # wrapper and the asynchronous flip, must be repaired in place.
            legacy = json.loads(config.read_text(encoding="utf-8"))
            for entry in legacy["hooks"].values():
                for item in entry[0]["hooks"]:
                    item["shell"] = "powershell"
                    item["async"] = True
            config.write_text(json.dumps(legacy), encoding="utf-8")
            repaired = install_native_hook("claude-code", config_path=config)
            self.assertTrue(repaired["changed"])
            for entry in json.loads(config.read_text(encoding="utf-8"))["hooks"].values():
                for item in entry[0]["hooks"]:
                    self.assertNotIn("shell", item)
                    self.assertIs(item["async"], False)

            settled = install_native_hook("claude-code", config_path=config)
            self.assertFalse(settled["changed"])

    def test_codex_hook_encodes_pinned_python_paths_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "hooks.json"
            install_native_hook("codex", config_path=config)
            installed = json.loads(config.read_text(encoding="utf-8"))
            commands = [
                item["commandWindows"]
                for event in installed["hooks"].values()
                for item in event[0]["hooks"]
            ]
            self.assertTrue(commands)
            if os.name == "nt":
                from agent_knowledge_bridge.integration_installation import prepare_hook_installation
                from agent_knowledge_bridge.integration_profiles import PROFILES
                plan = prepare_hook_installation(PROFILES['codex'], config_path=config)
                for command in commands:
                    self.assertTrue(command.startswith(
                        'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand '))
                    code = base64.b64decode(command.split()[-1], validate=True).decode('utf-16le')
                    self.assertIn(str(plan.python).replace("'", "''"), code)
                    self.assertIn(str(plan.launcher_path).replace("'", "''"), code)
                    self.assertTrue(code.endswith('; exit $LASTEXITCODE'))
                    self.assertNotIn('py.exe', code)

    def test_registered_agent_exposes_hook_and_latest_learning_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "agents.db")
            store.register_agent(
                agent_id="claude-code",
                display_name="Claude Code",
                adapter_type="claude-hook",
                installed=True,
                detected_by=["config"],
            )
            service = KnowledgeBridgeService(
                agent_id="console", project_key="test", database_path=Path(temp) / "agents.db"
            )
            item = service.list_agents(include_disabled=True)[0]
            self.assertIn("hook", item)
            self.assertIsNone(item["learning"])

    def test_runtime_agent_endpoints_support_discovery_register_and_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = TestClient(
                create_app(
                    database_path=Path(temp) / "runtime.db",
                    api_token="agent-test-token",
                )
            )
            headers = {"Authorization": "Bearer agent-test-token"}
            discovered = client.get("/v1/agents/discover", headers=headers)
            self.assertEqual(discovered.status_code, 200)
            self.assertIn("codex", {item["agent_id"] for item in discovered.json()["results"]})

            registered = client.post(
                "/v1/agents/register",
                headers=headers,
                json={
                    "agent_id": "custom-reviewer",
                    "display_name": "Custom Reviewer",
                    "adapter_type": "custom",
                    "capabilities": ["shared-knowledge"],
                },
            )
            self.assertEqual(registered.status_code, 200)
            self.assertTrue(registered.json()["agent"]["enabled"])

            listed = client.get("/v1/agents", headers=headers).json()
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["results"][0]["agent_id"], "custom-reviewer")

            disabled = client.post(
                "/v1/agents/disable",
                headers=headers,
                json={"agent_id": "custom-reviewer"},
            )
            self.assertEqual(disabled.status_code, 200)
            self.assertFalse(disabled.json()["agent"]["enabled"])
            self.assertEqual(client.get("/v1/agents", headers=headers).json()["count"], 0)
            self.assertEqual(
                client.get("/v1/agents?include_disabled=true", headers=headers).json()["count"],
                1,
            )

            invalid = client.post(
                "/v1/agents/register",
                headers=headers,
                json={"agent_id": "bad id", "display_name": "Invalid"},
            )
            self.assertEqual(invalid.status_code, 422)
            client.close()


if __name__ == "__main__":
    unittest.main()
