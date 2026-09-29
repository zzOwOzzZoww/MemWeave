# Claude Code Private Policy Input

The JSON below is the only authoritative policy for this demo. Codex cannot read this file directly and must retrieve the knowledge published by Claude Code through MemWeave.

```json
{
  "demo_id": "MW-DEMO-HANDOFF-20260918",
  "policy_name": "evidence-first",
  "max_candidates": 5,
  "archive_ttl_days": 14,
  "promote_on": ["user_approved", "test_verified"],
  "default_search_scope": "active_only",
  "archived_recall": "explicit_user_intent"
}
```
