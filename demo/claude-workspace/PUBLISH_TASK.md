# Stage One: Publish the Policy

1. Read `source-policy.md`.
2. Call `knowledge_capabilities` and confirm that the current identity is `claude-code`.
3. Use `knowledge_publish` to publish one project-scoped `decision`:
   - The title must contain `MW-DEMO-HANDOFF-20260918`.
   - The content must preserve every field and value from the policy JSON.
   - Set the evidence summary to `source-policy.md is the authoritative Demo input read by Claude Code in this stage.`
4. Confirm the new record has `status=candidate`, then report the knowledge ID and `source_agent`.

Do not read or modify `codex-workspace`. Do not access SQLite directly.
