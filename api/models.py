"""API request/response models. Agent objects are the pydantic models from agents/contracts.py;
these only wrap them for transport (plan §8 routes)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.contracts import (
    ApprovalChecklist, ApprovalDecision, AuditEvent, CheckResult, DependencyProfile, DraftPR, Forecast,
    Recommendation, RiskSignal, Scenario,
)
from agents.explain import Explanation

MAX_FORMS_PER_RUN = 50  # keep runs light on the shared HANA instance


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(_Model):
    status: Literal["ok"] = "ok"
    service: str = "flowforge-api"
    db_backend: str
    version: str


class WatchlistRow(_Model):
    """One FF_V_WATCHLIST row. Producer counts are lower bounds; flags are probabilistic, for review."""

    form_id: str
    generic: str | None = None
    dosage_form: str | None = None
    strength: str | None = None
    nlem_level: str | None = None
    therapeutic_class: str | None = None
    ceiling_price_inr: float | None = None
    gst_rate: float | None = None
    ceiling_so_number: str | None = None
    producer_count_min: int = 0
    producers_label: str
    min_days_of_cover: float | None = None
    run_id: str | None = None
    assessed_at: str | None = None
    exit_risk_band: str | None = None
    exit_risk: float | None = None
    exposure: float | None = Field(default=None, description="A3 exposure (risk x criticality x cover gap); sort key")
    window_months_lo: float | None = None
    window_months_hi: float | None = None
    cause_code: str | None = None
    confidence: float | None = None
    margin_band: str | None = None
    headroom_pct: float | None = None
    months_to_breach: float | None = None
    hhi: float | None = None
    top_origin_country: str | None = None
    top_origin_share: float | None = None
    data_tag: str | None = Field(default=None, description="IS_PROXY of the formulation row: REAL / PROXY / SYNTH")


class GraphNode(_Model):
    id: str
    type: str
    label: str


class GraphEdge(_Model):
    src: str
    dst: str
    rel: str
    weight: float | None = None
    is_proxy: str | None = None


class GraphSnapshot(_Model):
    engine: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class RecommendationView(_Model):
    recommendation: Recommendation
    scenario: Scenario | None = None


class MoleculeDetail(_Model):
    form_id: str
    formulation: dict[str, Any]
    run_id: str | None = None
    signal: RiskSignal | None = None
    dependency: DependencyProfile | None = None
    forecast: Forecast | None = None
    scenarios: list[Scenario] = Field(default_factory=list)
    recommendations: list[RecommendationView] = Field(default_factory=list)
    graph: GraphSnapshot
    explanation: Explanation | None = Field(default=None, description="plain-language why; numbers checked")
    data_tags: dict[str, str] = Field(default_factory=dict)
    disclaimer: str


class MarginPoint(_Model):
    """A1 replayed as of one month (no look-ahead): what the Margin Sentinel would have reported then."""

    month: str
    realisation_inr: float | None = Field(default=None, description="est. manufacturer realisation under the ceiling")
    unit_cost_inr: float | None = None
    headroom_pct: float | None = None
    band: str
    shocked_unit_cost_inr: float | None = None
    shocked_headroom_pct: float | None = None


class MarginSeries(_Model):
    form_id: str
    ceiling_price_inr: float | None = Field(default=None, description="current NPPA ceiling, excl. GST")
    shock: float | None = None
    points: list[MarginPoint]
    data_tags: dict[str, str] = Field(default_factory=dict)
    note: str = "Estimated from NPPA ceiling, API import cost and BOM assumptions. A risk flag for review."


class RunRequest(_Model):
    form_ids: list[str] = Field(default_factory=list, max_length=MAX_FORMS_PER_RUN,
                                description="formulation ids or generic names; empty = tracked set")
    shock: float = Field(default=1.0, gt=0, le=5, description="API cost multiplier, 1.3 = +30%")
    dry_run: bool = False


class RunResponse(_Model):
    run_id: str
    status: str
    dry_run: bool
    chain_ok: bool
    signals: list[RiskSignal]
    forecasts: list[Forecast]
    recommendations: list[Recommendation]
    awaiting_approval: list[str]
    explanations: dict[str, Explanation] = Field(default_factory=dict)


class ShockRequest(_Model):
    multiplier: float = Field(gt=0, le=5, description="API cost multiplier, 1.3 = +30%")
    form_ids: list[str] = Field(default_factory=list, max_length=200, description="empty = tracked set")


class ShockRow(_Model):
    form_id: str
    generic: str | None = None
    strength: str | None = None
    baseline: RiskSignal
    shocked: RiskSignal
    band_changed: bool


class ShockResponse(_Model):
    multiplier: float
    rows: list[ShockRow]
    note: str = "What-if on A1 only; nothing is written. Probabilistic flags for review."


class PendingApproval(_Model):
    rec_id: str
    run_id: str
    form_id: str
    generic: str | None = None
    overall: str
    needs_second_approver: bool
    awaiting_level: Literal[1, 2]
    scenario: Scenario | None = None
    draft_pr: DraftPR | None = None
    checks: list[CheckResult] = Field(default_factory=list)


class Edits(_Model):
    qty: float | None = Field(default=None, gt=0)
    supplier: str | None = None


class ApprovalRequest(_Model):
    rec_id: str
    decision: str = Field(description="APPROVE | EDIT_APPROVE | REJECT | SNOOZE")
    user: str = Field(min_length=1, max_length=120)
    checklist: ApprovalChecklist = Field(default_factory=ApprovalChecklist)
    edits: Edits | None = None
    reason: str | None = Field(default=None, max_length=1000)
    level: Literal[1, 2] = 1
    snooze_days: int | None = Field(default=None, gt=0)


class ApprovalResponse(_Model):
    rec_id: str
    status: str
    decision: ApprovalDecision
    banfn: str | None = None
    payload: dict[str, Any] | None = None
    master_data: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ChainStatus(_Model):
    ok: bool
    n_rows: int
    broken_at: int | None = None
    reason: str = ""


class AuditResponse(_Model):
    run_id: str
    run: dict[str, Any] | None = None
    chain: ChainStatus
    events: list[AuditEvent]


class RecallResponse(_Model):
    alert_id: str
    alert: dict[str, Any]
    matches: list[dict[str, Any]]
    note: str = "Batch matches on the hospital's stock; verify physically before quarantine."
