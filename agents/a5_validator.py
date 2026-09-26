"""A5 Feasibility & Policy Validator: runs every check in rules/dpco.yaml and rules/sop_controls.yaml
on each ranked A4 scenario before a human sees it (plan §3 A5).

Each check returns PASS, or its YAML `on_fail` severity (FAIL/WARN) when violated, or an explicit
WARN for "can't confirm" cases. Evidence holds the numbers compared; rule_source cites the rule.
    overall = BLOCKED if any FAIL, NEEDS_CHANGES if any WARN, else READY
Second approver: PR value above the BUDGET threshold, or any therapeutic substitute (human gate).
Price: Scenario.unit_rate_inr is GST-inclusive and must be <= ceiling × (1 + GST) (DPCO para 14).
Writes FF_AG_RECOMMENDATION and FF_AG_CHECK (S01 columns).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Sequence

import pandas as pd

from agents.common import placeholders, resolve_ctx, try_query
from agents.contracts import (
    CheckResult, CheckStatus, DraftPR, Forecast, OptionType, Overall, Recommendation, Scenario,
)
from agents.ctx import Db
from agents.rules import Rules

AGENT = "A5"
REC_COLS = ("RUN_ID", "SCENARIO_ID", "FORM_ID", "OVERALL", "NEEDS_SECOND_APPROVER", "DRAFT_PR_JSON", "CREATED_AT")
CHECK_COLS = ("RUN_ID", "SCENARIO_ID", "CHECK_ID", "NAME", "STATUS", "EVIDENCE_JSON", "RULE_SOURCE", "CREATED_AT")
BLOCKED_FLAGS = ("X", "Y", "1", "TRUE")


@dataclass
class _Env:
    db: Db
    rules: Rules
    as_of: date
    forecast: Forecast | None


def _form_of(s: Scenario) -> str:
    return s.substitute_formulation_id or s.formulation_id


def _num(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


# ---------------------------------------------------------------- checks


def check_price(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.unit_rate_inr is None:
        return True, {"note": "no purchase (internal transfer)"}
    cp = try_query(e.db, "SELECT EFFECTIVE_FROM, CEILING_PRICE, GST_RATE, SO_NUMBER FROM FF_REF_CEILING_PRICE "
                         "WHERE FORM_ID = ?", (_form_of(s),))
    if cp is not None and len(cp):
        cp = cp[pd.to_datetime(cp["EFFECTIVE_FROM"].astype(str)).dt.date <= e.as_of]
    if cp is None or cp.empty:
        return None, {"formulation": _form_of(s), "note": "no ceiling price on record"}
    r = cp.sort_values("EFFECTIVE_FROM").iloc[-1]
    gst = _num(r["GST_RATE"])
    gst = gst if gst is not None else float(e.rules.value("dpco", "constants.gst_rate_medicines"))
    ceiling = float(r["CEILING_PRICE"])
    cap = round(ceiling * (1 + gst), 4)
    rate = float(s.unit_rate_inr)
    return rate <= cap + 1e-9, {"unit_rate_incl_gst_inr": rate, "ceiling_excl_gst_inr": ceiling, "gst_rate": gst,
                                "max_allowed_inr": cap, "so_number": r["SO_NUMBER"]}


def check_scheduled(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    f = _form_of(s)
    fm = e.db.query("SELECT NLEM_LEVEL FROM FF_REF_FORMULATION WHERE FORM_ID = ?", (f,))
    so = e.db.query("SELECT SO_NUMBER FROM FF_REF_CEILING_PRICE WHERE FORM_ID = ? AND SO_NUMBER IS NOT NULL "
                    "AND SO_NUMBER <> ''", (f,))
    nlem = str(fm.iloc[0]["NLEM_LEVEL"]) if len(fm) and fm.iloc[0]["NLEM_LEVEL"] else None
    sos = sorted(set(so["SO_NUMBER"].astype(str)))
    return bool(nlem and sos), {"formulation": f, "nlem_level": nlem, "ceiling_so_numbers": sos}


def check_supplier(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.option_type is OptionType.REBALANCE or not s.supplier:
        return True, {"note": "no external supplier"}
    v = try_query(e.db, "SELECT NAME1, GSTIN, DRUG_LICENCE_NO, SPERR FROM FF_MM_LFA1 WHERE LIFNR = ?", (s.supplier,))
    if v is None or v.empty:
        return None, {"supplier": s.supplier, "note": "not in vendor master (FF_MM_LFA1); onboarding needed"}
    r = v.iloc[0]
    blocked = str(r["SPERR"] or "").strip().upper() in BLOCKED_FLAGS
    ok = bool(r["DRUG_LICENCE_NO"]) and bool(r["GSTIN"]) and not blocked
    return ok, {"supplier": s.supplier, "name": r["NAME1"], "drug_licence_no": r["DRUG_LICENCE_NO"],
                "gstin": r["GSTIN"], "blacklisted": blocked}


def check_quality(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.option_type is OptionType.REBALANCE or not s.supplier:
        return True, {"note": "no external supplier"}
    lookback = int(e.rules.param("QUALITY_HISTORY", "nsq_lookback_months"))
    nsq = try_query(e.db, "SELECT ALERT_ID, MONTH, FORM_ID FROM FF_REF_NSQ_ALERT WHERE MANUFACTURER_ID = ?",
                    (s.supplier,))
    if nsq is None or nsq.empty:
        return True, {"supplier": s.supplier, "nsq_alerts": 0}
    nsq = nsq[nsq["FORM_ID"].isna() | (nsq["FORM_ID"].astype(str) == _form_of(s))]
    m = pd.to_datetime(nsq["MONTH"].astype(str)).dt.to_period("M")
    recent = nsq[m > pd.Period(e.as_of, "M") - lookback]
    ev = {"supplier": s.supplier, "lookback_months": lookback, "recent_alerts": recent["ALERT_ID"].tolist(),
          "older_alerts": nsq.loc[~nsq.index.isin(recent.index), "ALERT_ID"].tolist()}
    if len(recent):
        return False, ev
    return (None if ev["older_alerts"] else True), ev


def check_shelf_life(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.option_type is OptionType.REBALANCE:
        return True, {"note": "existing stock"}
    need = float(e.rules.param("SHELF_LIFE", "min_residual_months"))
    if s.residual_shelf_life_months is None:
        return None, {"min_residual_months": need, "note": "offered shelf life unknown"}
    return s.residual_shelf_life_months >= need, {"offered_months": s.residual_shelf_life_months,
                                                   "min_residual_months": need}


def check_expiry(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    mx = float(e.rules.param("EXPIRY_WASTE", "max_expired_share"))
    return s.expiry_waste_risk <= mx, {"projected_expired_share": s.expiry_waste_risk, "max_share": mx}


def check_budget(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    budget = float(e.rules.param("BUDGET", "budget_remaining_inr"))
    thr = float(e.rules.param("BUDGET", "second_approver_threshold_inr"))
    cost = float(s.cost)
    return cost <= budget, {"pr_value_inr": cost, "budget_remaining_inr": budget,
                            "second_approver_threshold_inr": thr, "above_threshold": cost > thr}


def check_storage(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.option_type is OptionType.REBALANCE:
        return True, {"note": "no net new stock"}
    key = "cold_free_capacity_units" if s.cold_chain else "ambient_free_capacity_units"
    cap = float(e.rules.param("STORAGE", key))
    return s.qty <= cap, {"qty": s.qty, "cold_chain": s.cold_chain, "free_capacity_units": cap}


def check_rol(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    facts = e.forecast.cause_facts if e.forecast else {}
    daily = (facts.get("daily_consumption") or {}).get("value")
    usable = (facts.get("usable_stock_qty") or {}).get("value") or 0.0
    if not daily:
        return None, {"note": "no consumption forecast"}
    added = 0.0 if s.option_type is OptionType.REBALANCE else s.qty
    max_stock = float(daily) * float(e.rules.param("ROL_ROQ", "max_stock_days"))
    limit = max_stock * float(e.rules.param("ROL_ROQ", "max_stock_buffer_factor"))
    resulting = float(usable) + added
    return resulting <= limit, {"resulting_stock": round(resulting, 4), "max_stock": round(max_stock, 4),
                                "limit": round(limit, 4)}


def check_clinical(s: Scenario, e: _Env) -> tuple[bool | None, dict]:
    if s.option_type is OptionType.THERAPEUTIC_ALT:
        return False, {"substitute_formulation_id": s.substitute_formulation_id,
                       "note": "therapeutic substitute: pharmacist/clinician sign-off required"}
    return True, {"note": "same formulation"}


CHECKS: dict[str, Callable[[Scenario, _Env], tuple[bool | None, dict]]] = {
    "PRICE_COMPLIANCE": check_price, "SCHEDULED_STATUS": check_scheduled, "SUPPLIER_LICENCE": check_supplier,
    "QUALITY_HISTORY": check_quality, "SHELF_LIFE": check_shelf_life, "EXPIRY_WASTE": check_expiry,
    "BUDGET": check_budget, "STORAGE": check_storage, "ROL_ROQ": check_rol, "CLINICAL": check_clinical,
}


# ---------------------------------------------------------------- validate


def overall_of(checks: list[CheckResult]) -> Overall:
    st = {c.status for c in checks}
    return Overall.BLOCKED if CheckStatus.FAIL in st else Overall.NEEDS_CHANGES if CheckStatus.WARN in st else Overall.READY


def validate(s: Scenario, forecast: Forecast | None, db: Db, rules: Rules, as_of: date) -> Recommendation:
    env = _Env(db, rules, as_of, forecast)
    checks = []
    for rule in rules.checks():
        fn = CHECKS.get(rule["id"])
        if fn is None:
            raise KeyError(f"no implementation for rule check {rule['id']}")
        ok, ev = fn(s, env)
        status = CheckStatus.PASS if ok else CheckStatus.WARN if ok is None else CheckStatus(rule["on_fail"])
        checks.append(CheckResult(check_id=rule["id"], name=rule["name"], status=status,
                                  evidence={"rule": rule["rule"], **ev}, rule_source=rule["source"]))
    budget_ev = next(c.evidence for c in checks if c.check_id == "BUDGET")
    second = bool(budget_ev["above_threshold"]) or s.option_type is OptionType.THERAPEUTIC_ALT
    return Recommendation(run_id=s.run_id, scenario_id=s.scenario_id, checks=checks, overall=overall_of(checks),
                          needs_second_approver=second, draft_pr=draft_pr(s, as_of, rules))


def draft_pr(s: Scenario, as_of: date, rules: Rules) -> DraftPR | None:
    """S/4-shaped PR item for purchase options. REBALANCE is an internal transfer (no PR)."""
    if s.option_type is OptionType.REBALANCE or s.qty <= 0 or not s.plant:
        return None
    material = s.material or f"NEW-{s.substitute_formulation_id or s.formulation_id}"
    return DraftPR(
        material=material, plant=s.plant, quantity=s.qty,
        delivery_date=as_of + timedelta(days=int(rules.value("weights", "exposure.lead_time_days"))),
        purchasing_group=str(rules.value("sop_controls", "defaults.purchasing_group")),
        fixed_supplier=s.supplier, unit_price_inr=s.unit_rate_inr,
    )


def _write(db: Db, recs: list[Recommendation], form_of: dict[str, str]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.executemany(
        f"INSERT INTO FF_AG_RECOMMENDATION ({', '.join(REC_COLS)}) VALUES ({placeholders(REC_COLS)})",
        [(r.run_id, r.scenario_id, form_of[r.scenario_id], r.overall.value, int(r.needs_second_approver),
          json.dumps(r.draft_pr.to_s4_payload()) if r.draft_pr else None, now) for r in recs])
    db.executemany(
        f"INSERT INTO FF_AG_CHECK ({', '.join(CHECK_COLS)}) VALUES ({placeholders(CHECK_COLS)})",
        [(r.run_id, r.scenario_id, c.check_id, c.name, c.status.value, json.dumps(c.evidence, default=str),
          c.rule_source, now) for r in recs for c in r.checks])


def run(scenarios: Sequence[Scenario], forecasts: Sequence[Forecast], ctx: Any) -> list[Recommendation]:
    """Validate every ranked scenario (rejected ones, rank None, are skipped). Writes FF_AG_RECOMMENDATION/CHECK."""
    c = resolve_ctx(ctx, "a5")
    as_of = c.run.as_of or datetime.now(timezone.utc).date()
    fc = {f.formulation_id: f for f in forecasts}
    ranked = [s for s in scenarios if s.rank is not None]
    recs = [validate(s, fc.get(s.formulation_id), c.db, c.rules, as_of) for s in ranked]
    if recs and not c.run.dry_run:
        _write(c.db, recs, {s.scenario_id: s.formulation_id for s in ranked})
    return recs
