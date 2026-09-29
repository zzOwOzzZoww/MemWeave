# MemWeave Demo: Claude Code

This is an isolated bidirectional knowledge handoff demo.

- Treat the policy marked `MW-DEMO-HANDOFF-20260918` in `source-policy.md` as the only authority for the publish stage.
- Use the `agent_knowledge_bridge` MCP to publish knowledge. Never access SQLite directly as a substitute.
- During acceptance, retrieve the Codex receipt only through MCP. Do not read `codex-workspace`.
- Submit `verified` feedback only after the local verifier returns PASS.
- Never publish secrets, environment variables, raw conversations, or unsupported guesses.
