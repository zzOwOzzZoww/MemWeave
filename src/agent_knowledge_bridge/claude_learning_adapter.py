"""Compatibility facade for the Claude transcript format and hook response."""
from __future__ import annotations

from .claude_transcript import parse_latest_turn
from .integration_profiles import PROFILES
from .learning_engine import DeepSeekReviewer, LearningEngine, REVIEW_SYSTEM_PROMPT, SECRET_STORAGE_REQUEST


class ClaudeLearningAdapter(LearningEngine):
    def __init__(self, **kwargs):
        kwargs.setdefault("transcript_parser", parse_latest_turn)
        kwargs.setdefault("bind_transcript_boundary", kwargs.get("agent_id") == "claude-code")
        super().__init__(**kwargs)

    def recall(self, hook_input):
        return PROFILES["claude-code"].encode_context(self.recall_context(hook_input))
