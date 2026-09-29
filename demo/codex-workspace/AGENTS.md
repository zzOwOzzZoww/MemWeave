# MemWeave Demo: Codex

This is an isolated Codex side of the bidirectional MemWeave adapter demo.

- Do not read `../claude-workspace` or the MemWeave SQLite database directly.
- Validated context may be injected automatically by the project-level Codex Hook; MCP is optional and is not required for memory recall or learning.
- Treat injected `<memweave_context>` as a lead, then run the local verifier before reporting success.
- Never publish secrets, environment variables, raw conversations, or unsupported guesses.
