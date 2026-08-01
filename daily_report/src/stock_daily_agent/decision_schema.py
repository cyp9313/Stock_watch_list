"""Strict, auditable data contract for the AI Stock Report decision dashboard.

The dashboard is deliberately separate from the existing deterministic rating.
The ``final_score`` field is copied from Python's authoritative rating and is
never calculated by an LLM.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DecisionAction = Literal["buy", "add", "hold", "reduce", "sell", "watch", "avoid"]
OpinionAgentName = Literal["technical", "news_fundamental", "risk"]
OpinionSignal = Literal["buy", "hold", "sell"]


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


class OpinionAgentOutput(BaseModel):
    """A bounded advisory opinion; it can never own score, levels, or HTML."""

    model_config = ConfigDict(extra="forbid")

    agent: OpinionAgentName
    signal: OpinionSignal
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


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
    # These two fields are injected by Python after synthesis.  They make it
    # explicit that a score band and a user-context action are related but are
    # not interchangeable recommendations.
    authoritative_rating_class: Literal["buy", "hold", "avoid"] = "hold"
    authoritative_rating_text: str | None = Field(default=None, max_length=240)
    action_scope: Literal["no_position", "has_position"] = "no_position"
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
    agent_opinions: list[OpinionAgentOutput] = Field(default_factory=list, max_length=3)
    opinion_conflict_summary: str | None = Field(default=None, max_length=1000)
    opinion_agents_enabled: bool = False
    opinion_agents_completed: int = Field(default=0, ge=0, le=3)
    opinion_agents_unavailable: list[OpinionAgentName] = Field(default_factory=list, max_length=3)
    # Set only when Python replaces a model-written risk-boundary phrase that
    # conflicts with the deterministic stop-loss reference.
    risk_boundary_guardrail_applied: bool = False
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
