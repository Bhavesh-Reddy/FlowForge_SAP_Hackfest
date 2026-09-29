"""A3 Shortage Forecaster: when could supply at our hospital fail, and why? (plan §3 A3, §4.3)

    exit_risk = sigmoid( a·(−headroom) + b·(1/max(months_to_breach, floor)) + c·conc + d·nsq_recent + e·otd_drop )
    exposure  = exit_risk × criticality × clamp( (lead_time + requal) / max(days_of_cover, 1), 0, 3 )
    criticality = VED weight × nlem_core_multiplier if NLEM primary level

Consumption per hospital material: HANA PAL single exponential smoothing (hana-ml) when the
backend is HANA and PAL is installed; else statsmodels exponential smoothing on monthly issues;
with < 6 complete months of history, a 90-day moving average. The method is recorded.

Days of cover = usable stock (FF_V_DAYS_OF_COVER.USABLE_QTY, or non-expired and non-quarantined
FF_MM_MCHB batches) ÷ forecast daily consumption.

Inputs are the A1 RiskSignal and A2 DependencyProfile per formulation (passed by the orchestrator).
cause_facts holds every number used, with its DataTag, so the LLM can only narrate these facts.
A probabilistic flag for review; never a claim about a manufacturer. Writes FF_AG_FORECAST.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

from agents.common import AgentCtx, placeholders, resolve_ctx, to_tag, try_query, worst_tag
from agents.contracts import (
    CauseCode, DataTag, DependencyProfile, Forecast, RiskSignal, SignalBand,
)
from agents.ctx import Db
from agents.rules import Rules

AGENT = "A3"
ISSUE_MOVEMENTS = ("201", "261")
# Tie-break order when two causes contribute equally (most direct economic cause first).
CAUSE_ORDER = (
    CauseCode.CEILING_BELOW_COST, CauseCode.HEADROOM_ERODING, CauseCode.FEW_PRODUCERS,
    CauseCode.SINGLE_ORIGIN_API, CauseCode.QUALITY_NSQ, CauseCode.SUPPLIER_OTD_DROP,
)

FORECAST_COLS = (  # S01 db/schema.sql; the consumption method is in CAUSE_FACTS_JSON
    "RUN_ID", "FORM_ID", "EXIT_RISK_BAND", "EXIT_RISK", "EXPOSURE", "WINDOW_MONTHS_LO", "WINDOW_MONTHS_HI",
    "DAYS_OF_COVER", "STOCKOUT_DATE_IF_EXIT", "CAUSE_CODE", "CAUSE_FACTS_JSON", "CONFIDENCE", "CREATED_AT",
)


def _w(rules: Rules, path: str) -> Any:
    return rules.value("weights", path)


def _fact(value: Any, tag: DataTag | None, source: str, unit: str | None = None) -> dict[str, Any]:
    if isinstance(value, float):
        value = None if not math.isfinite(value) else round(value, 6)
    f = {"value": value, "tag": tag.value if tag else None, "source": source}
    if unit:
        f["unit"] = unit
    return f


# ---------------------------------------------------------------- consumption forecast


@dataclass
class Consumption:
    daily: float | None
    method: str  # PAL_SES | ETS | MA90 | NONE
    months_history: int


def monthly_issues(issues: pd.DataFrame, as_of: date) -> pd.Series:
    """Monthly issued qty over complete months before as_of, gaps filled with 0."""
    if issues.empty:
        return pd.Series(dtype=float)
    d = issues.assign(_D=pd.to_datetime(issues["BUDAT"].astype(str)))
    d = d[d["_D"].dt.date <= as_of]
    last_full = pd.Period(as_of, "M") - 1
    d = d[d["_D"].dt.to_period("M") <= last_full]
    if d.empty:
        return pd.Series(dtype=float)
    s = d.groupby(d["_D"].dt.to_period("M"))["MENGE"].sum().astype(float)
    idx = pd.period_range(s.index.min(), last_full, freq="M")
    return s.reindex(idx, fill_value=0.0)


def _ets_next_month(monthly: pd.Series) -> float:
    from statsmodels.tsa.holtwinters import SimpleExpSmoothing

    fit = SimpleExpSmoothing(monthly.to_numpy(dtype=float), initialization_method="estimated").fit()
    return max(0.0, float(fit.forecast(1)[0]))


def _pal_next_month(monthly: pd.Series) -> float:
    """HANA PAL single exponential smoothing via hana-ml. Opens its own ConnectionContext from env."""
    from hana_ml.algorithms.pal.tsa.exponential_smoothing import SingleExponentialSmoothing
    from hana_ml.dataframe import ConnectionContext, create_dataframe_from_pandas

    from agents.ctx import load_settings

    s = load_settings()
    cc = ConnectionContext(address=s.hana_host, port=s.hana_port, user=s.hana_user, password=s.hana_password,
                           encrypt=True, currentSchema=s.hana_schema or None)
    try:
        pdf = pd.DataFrame({"ID": range(1, len(monthly) + 1), "VALUE": monthly.to_numpy(dtype=float)})
        hdf = create_dataframe_from_pandas(cc, pdf, "#FF_A3_TS", force=True, table_type="LOCAL TEMPORARY")
        out = SingleExponentialSmoothing(forecast_num=1).fit_predict(hdf, key="ID").collect()
        return max(0.0, float(out.sort_values(out.columns[0]).iloc[-1, 1]))
    finally:
        cc.close()


def pal_available(db: Db) -> bool:
    """True if the backend is HANA, PAL (AFLPAL) is installed and hana-ml imports (Day-1 check, at runtime)."""
    if db.backend != "hana":
        return False
    try:
        import hana_ml  # noqa: F401

        if int(db.query("SELECT COUNT(*) AS N FROM SYS.AFL_AREAS WHERE AREA_NAME = 'AFLPAL'").iloc[0, 0]) == 0:
            return False
        # Installed is not enough: without the execute role every PAL call fails (the shared Hackfest user
        # has only PUBLIC), and each failed attempt costs a new hana-ml connection per material.
        role = db.query("SELECT COUNT(*) AS N FROM SYS.EFFECTIVE_ROLES WHERE USER_NAME = CURRENT_USER "
                        "AND ROLE_NAME LIKE 'AFL%AFLPAL%EXECUTE%'")
        return int(role.iloc[0, 0]) > 0
    except Exception:
        return False


def forecast_consumption(issues: pd.DataFrame, as_of: date, rules: Rules, use_pal: bool = False) -> Consumption:
    """Forecast daily consumption of one material from its goods issues (MENGE, BUDAT)."""
    monthly = monthly_issues(issues, as_of)
    months = int(len(monthly))
    if months >= int(_w(rules, "forecast.min_months_for_ets")):
        nxt = pd.Period(as_of, "M")
        if use_pal:
            try:
                return Consumption(_pal_next_month(monthly) / nxt.days_in_month, "PAL_SES", months)
            except Exception:
                pass  # recorded as ETS below; PAL failure must not stop the run
        return Consumption(_ets_next_month(monthly) / nxt.days_in_month, "ETS", months)
    days = int(_w(rules, "forecast.moving_average_days"))
    if issues.empty:
        return Consumption(None, "NONE", months)
    d = pd.to_datetime(issues["BUDAT"].astype(str)).dt.date
    window = issues[(d > as_of - timedelta(days=days)) & (d <= as_of)]
    return Consumption(float(window["MENGE"].astype(float).sum()) / days, f"MA{days}", months)


def days_of_cover(usable_qty: float, daily: float | None, rules: Rules) -> float | None:
    if daily is None:
        return None
    cap = float(_w(rules, "forecast.max_days_of_cover"))
    return cap if daily <= 0 else min(cap, usable_qty / daily)


# ---------------------------------------------------------------- §4.3 scoring


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


HEADROOM_BUFFER = "HEADROOM_BUFFER"  # a·(−headroom) when headroom >= 0: lowers risk, never a cause


def contributions(sig: RiskSignal, dep: DependencyProfile, rules: Rules) -> dict[str, float]:
    """Each additive term inside the §4.3 sigmoid, keyed by cause code (plus HEADROOM_BUFFER).

    Concentration is split into its §4.2 parts: producers (1/n, HHI) -> FEW_PRODUCERS, origin -> SINGLE_ORIGIN_API.
    """
    h = sig.headroom_pct if math.isfinite(sig.headroom_pct) else 0.0
    a = float(_w(rules, "exit_risk.a_neg_headroom"))
    floor = float(_w(rules, "exit_risk.months_to_breach_floor"))
    inv_mtb = 1 / max(sig.months_to_breach, floor) if sig.months_to_breach is not None else 0.0
    c = float(_w(rules, "exit_risk.c_concentration"))
    n = dep.n_producers_min
    producer_part = (_w(rules, "concentration.w_inv_producers") * (1 / n if n > 0 else 1.0)
                     + _w(rules, "concentration.w_hhi") * dep.hhi)
    origin_part = _w(rules, "concentration.w_top_origin_share") * dep.top_origin_share
    conc = dep.concentration if dep.concentration is not None else min(1.0, producer_part + origin_part)
    scale = conc / (producer_part + origin_part) if producer_part + origin_part > 0 else 0.0  # respect the [0,1] clamp
    nsq = 1.0 if any(s.kind == "NSQ" and s.value > 0 for s in sig.secondary_signals) else 0.0
    otd = max((s.value for s in sig.secondary_signals if s.kind == "OTD"), default=0.0) / 100  # pts -> fraction
    trend_term = _w(rules, "exit_risk.b_inv_months_to_breach") * inv_mtb
    below = h < 0  # breach already happened: the months-to-breach term belongs to CEILING_BELOW_COST
    return {
        CauseCode.CEILING_BELOW_COST.value: a * -h + trend_term if below else 0.0,
        HEADROOM_BUFFER: 0.0 if below else a * -h,
        CauseCode.HEADROOM_ERODING.value: 0.0 if below else trend_term,
        CauseCode.FEW_PRODUCERS.value: c * producer_part * scale,
        CauseCode.SINGLE_ORIGIN_API.value: c * origin_part * scale,
        CauseCode.QUALITY_NSQ.value: _w(rules, "exit_risk.d_nsq_recent") * nsq,
        CauseCode.SUPPLIER_OTD_DROP.value: _w(rules, "exit_risk.e_otd_drop") * otd,
    }


def exit_risk(contrib: dict[str, float]) -> float:
    return sigmoid(sum(contrib.values()))


def dominant_cause(contrib: dict[str, float]) -> CauseCode:
    """Largest positive driver; ties go to the earlier entry in CAUSE_ORDER."""
    return max(CAUSE_ORDER, key=lambda k: (contrib[k.value], -CAUSE_ORDER.index(k)))


def risk_band(p: float, rules: Rules) -> SignalBand:
    if p >= _w(rules, "exit_risk.band_red"):
        return SignalBand.RED
    if p >= _w(rules, "exit_risk.band_amber"):
        return SignalBand.AMBER
    return SignalBand.GREEN


def criticality(ved: str | None, nlem_level: str | None, rules: Rules) -> float | None:
    if not ved or str(ved).upper() not in ("V", "E", "D"):
        return None
    w = float(_w(rules, f"criticality.{str(ved).upper()}"))
    core = nlem_level is not None and "P" in {p.strip().upper() for p in str(nlem_level).split(",")}
    return w * (float(_w(rules, "criticality.nlem_core_multiplier")) if core else 1.0)


def exposure(p: float, crit: float | None, cover: float | None, rules: Rules) -> float:
    """§4.3. No hospital material / no criticality -> 0 (the hospital does not stock it)."""
    if crit is None or cover is None:
        return 0.0
    lo, hi = float(_w(rules, "exposure.cover_ratio_min")), float(_w(rules, "exposure.cover_ratio_max"))
    floor = float(_w(rules, "exposure.days_of_cover_floor"))
    need = float(_w(rules, "exposure.lead_time_days")) + float(_w(rules, "exposure.requal_days"))
    return p * crit * min(hi, max(lo, need / max(cover, floor)))


def exit_window(sig: RiskSignal, conc: float, rules: Rules) -> tuple[float | None, float | None]:
    """(lo, hi) months until an exit becomes likely. hi adds the DPCO para 21(2) notice period."""
    notice = float(rules.value("dpco", "constants.discontinuation_notice_months"))
    if not math.isfinite(sig.headroom_pct):
        return None, None
    if sig.months_to_breach is None:
        return (0.0, notice) if sig.headroom_pct < 0 else (None, None)
    m = sig.months_to_breach
    return m * (1 - float(_w(rules, "window.conc_shrink")) * conc), m + notice


# ---------------------------------------------------------------- inputs


@dataclass
class _Hospital:
    materials: list[str]
    ved: str | None
    usable_qty: float
    usable_source: str
    consumption: Consumption


def _hospital(db: Db, form_id: str, materials: list[str], as_of: date, rules: Rules, use_pal: bool) -> _Hospital:
    mara = try_query(db, "SELECT MATNR, VED FROM FF_MM_MARA WHERE FORM_ID = ?", (form_id,))
    mats = sorted(set(materials) | (set(mara["MATNR"].astype(str)) if mara is not None else set()))
    if not mats:
        return _Hospital([], None, 0.0, "none", Consumption(None, "NONE", 0))
    veds = [str(v).upper() for v in (mara["VED"] if mara is not None else []) if v]
    ved = next((v for v in ("V", "E", "D") if v in veds), None)  # most critical material wins

    issues = try_query(db, f"SELECT MATNR, BWART, MENGE, BUDAT FROM FF_MM_MSEG WHERE MATNR IN ({placeholders(mats)})", mats)
    if issues is None:
        issues = pd.DataFrame(columns=["MATNR", "BWART", "MENGE", "BUDAT"])
    issues = issues[issues["BWART"].astype(str).isin(ISSUE_MOVEMENTS)].assign(MENGE=lambda d: d["MENGE"].astype(float))
    consumption = forecast_consumption(issues, as_of, rules, use_pal)

    view_as_of = try_query(db, "SELECT AS_OF FROM FF_V_ASOF")
    same_day = view_as_of is not None and len(view_as_of) and str(view_as_of.iloc[0, 0])[:10] == as_of.isoformat()
    view = try_query(db, f"SELECT MATNR, USABLE_QTY FROM FF_V_DAYS_OF_COVER WHERE MATNR IN ({placeholders(mats)})",
                     mats) if same_day else None
    if view is not None:
        usable, src = float(view["USABLE_QTY"].astype(float).sum()), "FF_V_DAYS_OF_COVER"
    else:  # run as-of differs from the view's FF_CFG_PARAM date (e.g. a backtest): same FEFO logic here
        b = db.query(f"SELECT MATNR, LGORT, CHARG, CLABS, VFDAT FROM FF_MM_MCHB WHERE MATNR IN ({placeholders(mats)})",
                     mats)
        usable, src = fefo_usable(b, as_of, consumption.daily), "FF_MM_MCHB (FEFO, unrestricted, non-expired)"
    return _Hospital(mats, ved, usable, src, consumption)


def fefo_usable(batches: pd.DataFrame, as_of: date, daily: float | None) -> float:
    """Unrestricted (CLABS), non-expired stock that can be consumed before it expires, first-expiry-first-out.

    Mirrors FF_V_BATCH_FEFO: usable = min(CLABS, max(0, daily × days_to_expiry − qty ahead in the queue)).
    """
    if batches.empty:
        return 0.0
    b = batches.assign(Q=batches["CLABS"].astype(float), EXP=pd.to_datetime(batches["VFDAT"].astype(str)).dt.date)
    b = b[(b["Q"] > 0) & (b["EXP"] > as_of)].sort_values(["EXP", "LGORT", "CHARG"])
    total, ahead = 0.0, 0.0
    for r in b.itertuples():
        q = r.Q if not daily or daily <= 0 else min(r.Q, max(0.0, daily * (r.EXP - as_of).days - ahead))
        total += q
        ahead += r.Q
    return total


def _reference(db: Db, form_ids: list[str]) -> dict[str, dict[str, Any]]:
    ph = placeholders(form_ids)
    out: dict[str, dict[str, Any]] = {f: {} for f in form_ids}
    cp = try_query(db, f"SELECT FORM_ID, EFFECTIVE_FROM, CEILING_PRICE, IS_PROXY FROM FF_REF_CEILING_PRICE "
                       f"WHERE FORM_ID IN ({ph})", form_ids)
    if cp is not None:
        for f, g in cp.sort_values("EFFECTIVE_FROM").groupby("FORM_ID"):
            r = g.iloc[-1]
            out[str(f)]["ceiling"] = (float(r["CEILING_PRICE"]), to_tag(r["IS_PROXY"]))
    fm = try_query(db, f"SELECT FORM_ID, NLEM_LEVEL FROM FF_REF_FORMULATION WHERE FORM_ID IN ({ph})", form_ids)
    if fm is not None:
        for r in fm.itertuples():
            out[str(r.FORM_ID)]["nlem_level"] = r.NLEM_LEVEL
    return out


# ---------------------------------------------------------------- assess (pure) and run


def assess(sig: RiskSignal, dep: DependencyProfile, hosp: _Hospital, ref: dict[str, Any],
           as_of: date, run_id: str, rules: Rules) -> Forecast:
    """Score one formulation from A1 + A2 outputs and hospital data. No DB access."""
    contrib = contributions(sig, dep, rules)
    p = exit_risk(contrib)
    cause = dominant_cause(contrib)
    conc = dep.concentration or 0.0
    lo, hi = exit_window(sig, conc, rules)
    cover = days_of_cover(hosp.usable_qty, hosp.consumption.daily, rules) if hosp.materials else None
    crit = criticality(hosp.ved, ref.get("nlem_level"), rules)
    expo = exposure(p, crit, cover, rules)

    a1_tags = sig.data_quality.tags
    cost_tag = worst_tag([t.value for k, t in a1_tags.items() if k in ("bom", "api_cost", "wpi")]) \
        if any(k in a1_tags for k in ("bom", "api_cost")) else None
    headroom_tag = worst_tag([t.value for t in a1_tags.values()]) if a1_tags else None
    dep_tags = dep.data_quality.tags if dep.data_quality else {}
    ceiling, ceiling_tag = ref.get("ceiling", (None, None))
    nsq = next((s for s in sig.secondary_signals if s.kind == "NSQ"), None)
    otd = next((s for s in sig.secondary_signals if s.kind == "OTD"), None)
    rules_src = "rules/weights.yaml"

    facts: dict[str, Any] = {
        "as_of": _fact(as_of.isoformat(), None, "run"),
        "headroom_pct": _fact(sig.headroom_pct, headroom_tag, "A1 (§4.1)", "fraction"),
        "headroom_trend_per_month": _fact(sig.headroom_trend, headroom_tag, "A1 (§4.1)", "fraction/month"),
        "months_to_breach": _fact(sig.months_to_breach, headroom_tag, "A1 (§4.1)", "months"),
        "ceiling_price_inr": _fact(ceiling, ceiling_tag, "FF_REF_CEILING_PRICE (excl. GST)", "INR/unit"),
        "realisation_inr": _fact(float(sig.realisation_inr) if sig.realisation_inr is not None else None,
                                 ceiling_tag, "A1 (§4.1)", "INR/unit"),
        "estimated_unit_cost_inr": _fact(float(sig.unit_cost_inr) if sig.unit_cost_inr is not None else None,
                                         cost_tag, "A1 (§4.1)", "INR/unit"),
        "n_producers_min": _fact(dep.n_producers_min, dep_tags.get("producers"), "A2 (at least N producers)"),
        "producer_hhi": _fact(dep.hhi, dep_tags.get("producers"), "A2 (§4.2)"),
        "top_origin_country": _fact(dep.top_origin_country, dep_tags.get("api_origin"), "A2"),
        "top_origin_share": _fact(dep.top_origin_share, dep_tags.get("api_origin"), "A2", "fraction"),
        "concentration": _fact(conc, None, "A2 (§4.2)"),
        "nsq_alerts_recent": _fact(nsq.value if nsq else 0.0, nsq.tag if nsq else None, "A1 secondary (CDSCO NSQ)"),
        "otd_drop_pts": _fact(otd.value if otd else 0.0, otd.tag if otd else None, "A1 secondary (FF_V_SUPPLIER_OTD)"),
        "hospital_materials": _fact(hosp.materials, DataTag.SYNTH if hosp.materials else None, "FF_MM_MARA"),
        "ved": _fact(hosp.ved, DataTag.SYNTH if hosp.ved else None, "FF_MM_MARA"),
        "nlem_level": _fact(ref.get("nlem_level"), None, "FF_REF_FORMULATION"),
        "criticality": _fact(crit, None, rules_src),
        "usable_stock_qty": _fact(hosp.usable_qty if hosp.materials else None,
                                  DataTag.SYNTH if hosp.materials else None, hosp.usable_source),
        "daily_consumption": _fact(hosp.consumption.daily, DataTag.SYNTH if hosp.consumption.daily is not None else None,
                                   "FF_MM_MSEG goods issues", "units/day"),
        "consumption_method": _fact(hosp.consumption.method, None, "A3"),
        "consumption_months_history": _fact(hosp.consumption.months_history, None, "A3"),
        "days_of_cover": _fact(cover, DataTag.SYNTH if cover is not None else None, "A3", "days"),
        "lead_time_days": _fact(float(_w(rules, "exposure.lead_time_days")), None, rules_src, "days"),
        "requal_days": _fact(float(_w(rules, "exposure.requal_days")), None, rules_src, "days"),
        "exit_risk": _fact(p, None, "A3 (§4.3)"),
        "exposure": _fact(expo, None, "A3 (§4.3)"),
        "window_months_lo": _fact(lo, None, "A3", "months"),
        "window_months_hi": _fact(hi, None, "A3", "months"),
        "contributions": {k: round(v, 6) for k, v in contrib.items()},
    }
    data_tags = [f["tag"] for f in facts.values() if isinstance(f, dict) and "tag" in f and f["tag"]]
    conf = sum(1 for t in data_tags if t == DataTag.REAL.value) / len(data_tags) if data_tags else 0.0

    return Forecast(
        run_id=run_id,
        formulation_id=sig.formulation_id,
        exit_risk_band=risk_band(p, rules),
        exit_risk=round(p, 6),
        exposure=round(expo, 6),
        window_months_lo=None if lo is None else round(lo, 4),
        window_months_hi=None if hi is None else round(hi, 4),
        days_of_cover=None if cover is None else round(cover, 4),
        stockout_date_if_exit=None if cover is None else as_of + timedelta(days=int(cover)),
        cause_code=cause,
        cause_facts=facts,
        confidence=round(conf, 4),
    )


def _write(db: Db, forecasts: list[Forecast]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (f.run_id, f.formulation_id, f.exit_risk_band.value, f.exit_risk, f.exposure, f.window_months_lo,
         f.window_months_hi, f.days_of_cover, f.stockout_date_if_exit.isoformat() if f.stockout_date_if_exit else None,
         f.cause_code.value, json.dumps(f.cause_facts), f.confidence, now)
        for f in forecasts
    ]
    db.executemany(f"INSERT INTO FF_AG_FORECAST ({', '.join(FORECAST_COLS)}) VALUES ({placeholders(FORECAST_COLS)})", rows)


def run(signals: Sequence[RiskSignal], deps: Sequence[DependencyProfile], ctx: Any) -> list[Forecast]:
    """One Forecast per formulation present in both `signals` (A1) and `deps` (A2), sorted by exposure (§4.4)."""
    c: AgentCtx = resolve_ctx(ctx, "a3")
    as_of = c.run.as_of or datetime.now(timezone.utc).date()
    dep_by = {d.formulation_id: d for d in deps}
    pairs = [(s, dep_by[s.formulation_id]) for s in signals if s.formulation_id in dep_by]
    if not pairs:
        return []
    refs = _reference(c.db, [s.formulation_id for s, _ in pairs])
    use_pal = pal_available(c.db)
    out = [
        assess(s, d, _hospital(c.db, s.formulation_id, d.affected_materials, as_of, c.rules, use_pal),
               refs.get(s.formulation_id, {}), as_of, c.run.run_id, c.rules)
        for s, d in pairs
    ]
    out.sort(key=lambda f: (-(f.exposure or 0.0), -(f.confidence or 0.0), f.formulation_id))
    if not c.run.dry_run:
        _write(c.db, out)
    return out
