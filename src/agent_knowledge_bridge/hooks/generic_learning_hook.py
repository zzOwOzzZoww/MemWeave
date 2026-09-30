"""Run an integration profile without adding an Agent-specific Python script."""
from __future__ import annotations

import argparse
import sys

from agent_knowledge_bridge.integration_profiles import PROFILES, load_profile
from agent_knowledge_bridge.hooks.shared_hook import compatibility_main, pin_streams


def main() -> int:
    pin_streams()
    parser = argparse.ArgumentParser(description="MemWeave declarative integration",
        epilog="Commands: hook (default), metrics, pending, approve, reject.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--agent", choices=tuple(PROFILES))
    selection.add_argument("--profile", help="Local JSON event/field/output mappings")
    args, command_args = parser.parse_known_args()
    try:
        profile = load_profile(args.profile) if args.profile else PROFILES[args.agent]
    except (OSError, ValueError):
        print("MemWeave integration profile is invalid; skipping memory", file=sys.stderr)
        print("{}")
        return 0
    return compatibility_main(profile, allow_review=True, allow_workspace=True, argv=command_args)


if __name__ == "__main__":
    raise SystemExit(main())
