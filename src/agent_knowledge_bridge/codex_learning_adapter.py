from __future__ import annotations

from agent_knowledge_bridge.learning_engine import LearningEngine
from agent_knowledge_bridge.integration_profiles import PROFILES
from agent_knowledge_bridge.codex_transcript import parse_latest_codex_turn


class CodexLearningAdapter(LearningEngine):
    """Codex adapter backed by the same learning engine and evidence gates."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs, transcript_parser=parse_latest_codex_turn)

    def recall(self, hook_input):
        return PROFILES["codex"].encode_context(self.recall_context(hook_input))
