# Stage Two: Retrieve, Execute, Verify, and Return

1. Call `knowledge_capabilities` and confirm that the current identity is `codex`.
2. Read the candidate queue with `knowledge_review_queue(status="candidate")`.
3. Select the candidate containing `MW-DEMO-HANDOFF-20260918` with `source_agent=claude-code` and `cross_agent=true`, then record its knowledge ID. Do not expect it in normal `knowledge_search` before verification.
4. Generate `handoff-result.json` in the current directory using only the knowledge content. Copy every policy field and value from the search result. Do not guess or use placeholders:

```json
{
  "demo_id": "read from knowledge content",
  "policy_name": "read from knowledge content",
  "max_candidates": "read from knowledge content and preserve the original type",
  "archive_ttl_days": "read from knowledge content and preserve the original type",
  "promote_on": "read from knowledge content and preserve the original type",
  "default_search_scope": "read from knowledge content",
  "archived_recall": "read from knowledge content",
  "source_knowledge_id": "actual policy knowledge ID",
  "source_agent": "claude-code",
  "cross_agent": true,
  "generated_by": "codex"
}
```

This task file provides no policy values other than the unique marker. If the search result does not contain the complete content, stop and report the problem. Do not fill values from this template.

5. Run `python ..\scripts\demo_control.py verify --stage handoff`.
6. Only after the command returns PASS, call `knowledge_feedback` for the policy knowledge ID with:
   - `outcome=verified`
   - `evidence_summary=handoff-result.json passed the local handoff verifier.`
   - `evidence_kind=test`
   - `evidence_ref=handoff-result.json`
   Confirm that the transition is `candidate → active`.
7. Use `knowledge_publish` to publish one project-scoped `fact` receipt:
   - The title must contain `MW-DEMO-RECEIPT-20260918`.
   - The content must contain `MW-DEMO-HANDOFF-20260918`, the original policy knowledge ID, `validator=PASS`, and `output=handoff-result.json`.
   - Set the evidence summary to `Codex executed the policy and passed the local handoff verifier.`
8. Confirm the receipt is a new `candidate`, then report the original policy knowledge ID, receipt knowledge ID, and resulting policy `verified_count`.

Do not read `../claude-workspace`. Do not access SQLite directly.
