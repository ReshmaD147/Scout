from __future__ import annotations

from dataclasses import dataclass

from scout.agents.intent_splitter import StructuredIntent


@dataclass(frozen=True)
class TurnExecutionContext:
    direct_specialist: str | None = None
    structured_intent: StructuredIntent | None = None
