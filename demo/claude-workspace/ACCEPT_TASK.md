# Stage Three: Accept the Codex Receipt

1. Call `knowledge_capabilities` and confirm that the current identity is `claude-code`.
2. Read the candidate queue with `knowledge_review_queue(status="candidate")` and select the record containing `MW-DEMO-RECEIPT-20260918`.
3. Confirm that the candidate has `source_agent=codex` and `cross_agent=true`. Do not expect it in normal `knowledge_search` before verification.
4. Create `acceptance-result.json` in the current directory with this shape:

```json
{
  "demo_id": "MW-DEMO-HANDOFF-20260918",
  "receipt_marker": "MW-DEMO-RECEIPT-20260918",
  "receipt_knowledge_id": "actual receipt knowledge ID",
  "source_agent": "codex",
  "cross_agent": true,
  "accepted_by": "claude-code"
}
```

5. Run `python ..\scripts\demo_control.py verify --stage acceptance`.
6. Only after the command returns PASS, call `knowledge_feedback` for the receipt knowledge ID with:
   - `outcome=verified`
   - `evidence_summary=acceptance-result.json passed the local acceptance verifier.`
   - `evidence_kind=test`
   - `evidence_ref=acceptance-result.json`
7. Confirm that the transition is `candidate → active`, then report the receipt knowledge ID and resulting `verified_count`.

Do not read `codex-workspace` or SQLite directly.
