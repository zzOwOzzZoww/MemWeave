"""Legacy Claude entry point; execution belongs to the shared integration layer."""
from __future__ import annotations

from agent_knowledge_bridge import runtime_state
from agent_knowledge_bridge.hooks import shared_hook
from agent_knowledge_bridge.integration_profiles import PROFILES


PROFILE = PROFILES["claude-code"]
selected_database = runtime_state.database_path
_daemon_is_live = shared_hook.daemon_is_live


def selected_agent():
    return runtime_state.agent_id(PROFILE.agent_id)


def selected_project(cwd=""):
    return runtime_state.project_key(cwd=cwd, agent=selected_agent())


def selected_runtime():
    return shared_hook.selected_runtime(PROFILE)


def append_error(message, hook_input=None):
    shared_hook.append_error(PROFILE, message, hook_input)


def run_hook():
    return shared_hook.run_hook(PROFILE, runtime_selector=selected_runtime)


def main():
    return shared_hook.compatibility_main(PROFILE, runtime_selector=selected_runtime, allow_review=True)


if __name__ == "__main__":
    raise SystemExit(main())
