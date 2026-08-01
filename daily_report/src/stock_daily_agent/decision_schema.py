"""Strict, auditable data contract for the AI Stock Report decision dashboard.

The dashboard is deliberately separate from the existing deterministic rating.
The ``final_score`` field is copied from Python's authoritative rating and is
never calculated by an LLM.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DecisionAction = Literal["buy", "add", "hold", "reduce", "sell", "watch", "avoid"]


class EvidenceBackedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1400)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


class PositionAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_position: str = Field(min_length=1, max_length=1200)
    has_position: str = Field(min_length=1, max_length=1200)


class TradingLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideal_buy: str | None = Field(default=None, max_length=160)
    secondary_buy: str | None = Field(default=None, max_length=160)
    stop_loss: str | None = Field(default=None, max_length=160)
    take_profit: str | None = Field(default=None, max_length=160)
    invalidation_condition: str = Field(min_length=1, max_length=1000)


class PhaseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1, max_length=32)
    timezone: str = Field(min_length=1, max_length=80)
    phase: str = Field(min_length=1, max_length=32)
    action_window: str = Field(min_length=1, max_length=400)
    immediate_action: str = Field(min_length=1, max_length=1000)
    watch_conditions: list[str] = Field(default_factory=list, max_length=8)
    next_check_time: str | None = Field(default=None, max_length=160)
    data_limitations: list[str] = Field(default_factory=list, max_length=8)


class DecisionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_action: DecisionAction
    to_action: DecisionAction
    reason: str = Field(min_length=1, max_length=800)


class DecisionDashboard(BaseModel):
    """Validated user-facing decision summary.

    Extra fields are forbidden so a model cannot silently add unreviewed
    claims to the serialized decision artifact.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    one_sentence: str = Field(min_length=1, max_length=1400)
    proposed_action: DecisionAction
    final_action: DecisionAction
    final_score: float = Field(ge=0, le=100)
    confidence: Literal["low", "medium", "high"]
    position_advice: PositionAdvice
    levels: TradingLevels
    catalysts: list[EvidenceBackedItem] = Field(default_factory=list, max_length=5)
    risk_alerts: list[EvidenceBackedItem] = Field(default_factory=list, max_length=5)
    action_checklist: list[str] = Field(default_factory=list, max_length=8)
    phase_decision: PhaseDecision
    score_explanation: str = Field(min_length=1, max_length=1400)
    adjustments: list[DecisionAdjustment] = Field(default_factory=list, max_length=12)
    fallback_used: bool = False
    fallback_reason: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def _validate_adjustment_chain(self) -> "DecisionDashboard":
        """Ensure audit adjustments describe a continuous action transition."""
        if not self.adjustments:
            if self.proposed_action != self.final_action:
                raise ValueError("final_action differs from proposed_action without an adjustment chain")
            return self

        expected = self.proposed_action
        for adjustment in self.adjustments:
            if adjustment.from_action != expected:
                raise ValueError("decision adjustments must form a continuous chain")
            expected = adjustment.to_action
        if expected != self.final_action:
            raise ValueError("decision adjustment chain must end at final_action")
        return self
