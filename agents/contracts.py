"""Agent inputs and outputs: the single source of truth (plan §3, §4).

Every agent is `run(input, ctx) -> output` over these models. Numbers are
computed by deterministic rules (`rules/*.yaml`); the LLM never fills them.
Money is INR with 4 decimals (HANA DECIMAL(15,4)).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(max_digits=15, decimal_places=4)]
Share = Annotated[float, Field(ge=0.0, le=1.0)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- enums


class SignalBand(str, Enum):
    """A1 signal bands (plan §4.1)."""

    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


class CauseCode(str, Enum):
    """A3 stated cause of the exit-risk flag (plan §3 A3)."""

    CEILING_BELOW_COST = "CEILING_BELOW_COST"
    HEADROOM_ERODING = "HEADROOM_ERODING"
    SINGLE_ORIGIN_API = "SINGLE_ORIGIN_API"
    FEW_PRODUCERS = "FEW_PRODUCERS"
    QUALITY_NSQ = "QUALITY_NSQ"
    SUPPLIER_OTD_DROP = "SUPPLIER_OTD_DROP"


class DataTag(str, Enum):
    """Provenance label shown next to every fact (plan §5; IS_PROXY column)."""

    REAL = "REAL"
    PROXY = "PROXY"
    SYNTH = "SYNTH"


class CheckStatus(str, Enum):
    """A5 per-check result (plan §3 A5)."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class Overall(str, Enum):
    """A5 overall verdict; only READY / NEEDS_CHANGES reach the human gate (plan §3 A5)."""

    READY = "READY"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    BLOCKED = "BLOCKED"


class OptionType(str, Enum):
    """A4 scenario option types (plan §3 A4 a–d)."""

    BUFFER = "BUFFER"
    ALT_SUPPLIER = "ALT_SUPPLIER"
    THERAPEUTIC_ALT = "THERAPEUTIC_ALT"
    REBALANCE = "REBALANCE"


class ApprovalAction(str, Enum):
    """Human-gate actions (plan §3 human gate)."""

    APPROVE = "APPROVE"
    EDIT_APPROVE = "EDIT_APPROVE"
    REJECT = "REJECT"
    SNOOZE = "SNOOZE"


class RunTrigger(str, Enum):
    """Why a run started (plan §3 A1 trigger)."""

    NIGHTLY = "NIGHTLY"
    ON_DEMAND = "ON_DEMAND"
    SHOCK = "SHOCK"


# ---------------------------------------------------------------- shared parts


class DataSourceRef(_Model):
    """Provenance of an input: SOURCE / SOURCE_URL / FETCHED_AT / IS_PROXY (plan §5)."""

    source: str
    source_url: str | None = None
    fetched_at: datetime | None = None
    is_proxy: DataTag


class DataQuality(_Model):
    """Confidence = share of REAL inputs × freshness (plan §4.3)."""

    confidence: Share
    tags: dict[str, DataTag] = Field(default_factory=dict)


class ShockSpec(_Model):
    """On-demand shock, e.g. API import cost +30% (plan §3 A1 trigger)."""

    api_cost_pct: float = 0.0
    api_ids: list[str] = Field(default_factory=list, description="empty = all APIs")
    label: str | None = None


class RunContext(_Model):
    """Per-run input shared by the orchestrator with every agent (plan §2, §3)."""

    run_id: str
    started_at: datetime = Field(default_factory=_utcnow)
    trigger: RunTrigger = RunTrigger.ON_DEMAND
    formulation_ids: list[str] = Field(default_factory=list, description="empty = all tracked")
    as_of: date | None = None
    shock: ShockSpec | None = None
    dry_run: bool = False
    rule_versions: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------- A1


class SecondarySignal(_Model):
    """A1 secondary inputs: NSQ alerts, supplier OTD, cold-chain excursions (plan §3 A1)."""

    kind: Literal["NSQ", "OTD", "COLD_CHAIN"]
    value: float
    tag: DataTag
    note: str | None = None


class RiskSignal(_Model):
    """A1 Margin Sentinel output → FF_AG_SIGNAL (plan §3 A1, §4.1)."""

    run_id: str
    formulation_id: str
    band: SignalBand
    headroom_pct: float
    headroom_trend: float = Field(description="OLS slope of headroom_pct per month")
    months_to_breach: float | None = Field(default=None, ge=0, description="only when trend < 0")
    realisation_inr: Money | None = None
    unit_cost_inr: Money | None = None
    secondary_signals: list[SecondarySignal] = Field(default_factory=list)
    data_quality: DataQuality


# ---------------------------------------------------------------- A2


class DependencyProfile(_Model):
    """A2 Dependency Mapper output → FF_AG_DEPENDENCY (plan §3 A2, §4.2).

    `n_producers_min` is a lower bound: render as "at least N producers".
    """

    run_id: str
    formulation_id: str
    n_producers_min: int = Field(ge=0)
    hhi: Share
    top_origin_country: str | None = None
    top_origin_share: Share = 0.0
    concentration: Share | None = None
    affected_materials: list[str] = Field(default_factory=list)
    affected_wards: list[str] = Field(default_factory=list)
    data_quality: DataQuality | None = None


# ---------------------------------------------------------------- A3


class Forecast(_Model):
    """A3 Shortage Forecaster output (plan §3 A3, §4.3). A probabilistic flag for review."""

    run_id: str
    formulation_id: str
    exit_risk_band: SignalBand
    exit_risk: Share | None = None
    exposure: float | None = Field(default=None, ge=0)
    window_months_lo: float | None = Field(default=None, ge=0)
    window_months_hi: float | None = Field(default=None, ge=0)
    days_of_cover: float | None = Field(default=None, ge=0)
    stockout_date_if_exit: date | None = None
    cause_code: CauseCode
    cause_facts: dict[str, Any] = Field(default_factory=dict)
    confidence: Share | None = None

    @model_validator(mode="after")
    def _window_order(self) -> "Forecast":
        lo, hi = self.window_months_lo, self.window_months_hi
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("window_months_lo must be <= window_months_hi")
        return self


# ---------------------------------------------------------------- A4


class Scenario(_Model):
    """A4 Resilience Simulator option → FF_AG_SCENARIO (plan §3 A4)."""

    scenario_id: str
    run_id: str
    formulation_id: str
    option_type: OptionType
    supplier: str | None = None
    material: str | None = None
    qty: float = Field(ge=0)
    unit_rate_inr: Money | None = Field(default=None, description="GST-inclusive rate the hospital pays per unit")
    cost: Money
    coverage_days: float = Field(ge=0)
    expiry_waste_risk: Share = 0.0
    correlated_risk_flag: bool = False
    correlated_risk_reason: str | None = None
    rank: int | None = Field(default=None, ge=1, description="None = rejected (not offered to the gate)")
    plant: str | None = None
    cold_chain: bool = False
    residual_shelf_life_months: float | None = Field(default=None, ge=0)
    substitute_formulation_id: str | None = Field(default=None, description="THERAPEUTIC_ALT: the substitute")


# ---------------------------------------------------------------- A5


class CheckResult(_Model):
    """One A5 check with evidence (plan §3 A5 table; rules/dpco.yaml, rules/sop_controls.yaml)."""

    check_id: str
    name: str
    status: CheckStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    rule_source: str | None = None


class DraftPR(_Model):
    """Purchase requisition item shaped on S/4HANA API_PURCHASEREQ_PROCESS_SRV
    (A_PurchaseReqnItem) (plan §3 A6). Aliases are the S/4 field names."""

    material: str = Field(alias="Material")
    plant: str = Field(alias="Plant")
    quantity: float = Field(gt=0, alias="RequestedQuantity")
    base_unit: str = Field(default="EA", alias="BaseUnit")
    delivery_date: date = Field(alias="DeliveryDate")
    purchasing_group: str = Field(alias="PurchasingGroup")
    fixed_supplier: str | None = Field(default=None, alias="FixedSupplier")
    unit_price_inr: Money | None = Field(default=None, alias="PurchaseRequisitionPrice")
    currency: str = Field(default="INR", alias="PurReqnItemCurrency")

    def to_s4_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json", exclude_none=True)


class Recommendation(_Model):
    """A5 Validator output (plan §3 A5). Only READY / NEEDS_CHANGES reach the gate."""

    run_id: str
    scenario_id: str
    checks: list[CheckResult]
    overall: Overall
    needs_second_approver: bool = False
    draft_pr: DraftPR | None = None

    @model_validator(mode="after")
    def _ready_has_no_fail(self) -> "Recommendation":
        if self.overall is Overall.READY and any(c.status is CheckStatus.FAIL for c in self.checks):
            raise ValueError("overall READY is not allowed when a check FAILs")
        return self

    @property
    def reaches_gate(self) -> bool:
        return self.overall in (Overall.READY, Overall.NEEDS_CHANGES)


# ---------------------------------------------------------------- human gate


class ApprovalChecklist(_Model):
    """Manual verification checklist; every item must be ticked to approve (plan §3 human gate)."""

    cp01_material_master: bool = False
    cp02_rol_roq_stock: bool = False
    cp04_supplier_verified: bool = False
    price_within_ceiling_gst: bool = False
    shelf_life_fefo_ok: bool = False
    cause_reviewed_not_claim: bool = False

    @property
    def complete(self) -> bool:
        return all(self.model_dump().values())


class ApprovalDecision(_Model):
    """Human decision → FF_AG_APPROVAL; A6 acts only on an approving row (plan §3 human gate, A6)."""

    run_id: str
    scenario_id: str
    approver: str
    level: Literal[1, 2] = 1
    action: ApprovalAction
    decided_at: datetime = Field(default_factory=_utcnow)
    checklist: ApprovalChecklist = Field(default_factory=ApprovalChecklist)
    reason: str | None = None
    edited_qty: float | None = Field(default=None, gt=0)
    edited_supplier: str | None = None
    snooze_days: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _action_rules(self) -> "ApprovalDecision":
        a = self.action
        if a in (ApprovalAction.APPROVE, ApprovalAction.EDIT_APPROVE) and not self.checklist.complete:
            raise ValueError("approval requires every checklist item to be ticked")
        if a is ApprovalAction.EDIT_APPROVE and self.edited_qty is None and not self.edited_supplier:
            raise ValueError("EDIT_APPROVE requires edited_qty or edited_supplier")
        if a is ApprovalAction.REJECT and not (self.reason and self.reason.strip()):
            raise ValueError("REJECT requires a reason")
        if a is ApprovalAction.SNOOZE and self.snooze_days is None:
            raise ValueError("SNOOZE requires snooze_days")
        return self

    @property
    def approves(self) -> bool:
        return self.action in (ApprovalAction.APPROVE, ApprovalAction.EDIT_APPROVE)


# ---------------------------------------------------------------- A6


class AuditEvent(_Model):
    """Append-only, hash-chained audit row → FF_AG_AUDIT_LOG (plan §3 A6)."""

    run_id: str
    seq: int = Field(ge=0)
    agent: str
    ts: datetime = Field(default_factory=_utcnow)
    input_hash: str
    output_hash: str
    rule_versions: dict[str, str] = Field(default_factory=dict)
    data_sources: list[DataSourceRef] = Field(default_factory=list)
    prev_hash: str | None = Field(default=None, description="None only for the first event of the chain")
    event_hash: str | None = None
