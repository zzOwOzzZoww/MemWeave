from __future__ import annotations

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_transcript import parse_latest_codex_turn


class CodexLearningAdapter(ClaudeLearningAdapter):
    """Codex adapter backed by the same learning engine and evidence gates."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs, transcript_parser=parse_latest_codex_turn)

