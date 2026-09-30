"""Gemini compatibility entry point for the shared integration executor."""
from agent_knowledge_bridge.hooks import shared_hook
from agent_knowledge_bridge.integration_profiles import PROFILES


def run_hook():
    return shared_hook.run_hook(PROFILES["gemini-cli"])


def main():
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
