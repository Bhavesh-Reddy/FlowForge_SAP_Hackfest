"""Per-decision audit report as HTML for NABH evidence (plan §3 A6 reports): who, what, why, sources, hashes.

    python -m agents.report --rec <run_id>:<form_id>:<nn> [--out reports/]

Self-contained HTML (inline CSS; one print button) so it saves to PDF from any browser.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agents import a6_audit as a6
from agents.common import try_query
from agents.ctx import Db, get_db

DISCLAIMER = ("This is a probabilistic risk flag for review, produced by deterministic rules. It is not a statement "
              "about any manufacturer's intent. Producer counts are lower bounds.")

CSS = """
:root { --navy:#12355B; --teal:#0F9D9A; --teal-l:#E8F8F7; --fg:#1F2D3D; --muted:#5B6B7B; --line:#D9E4EC; --bg:#F4F8FB;
        --pass:#1E7B55; --pass-l:#E6F7F0; --warn:#8A5A0B; --warn-l:#FEF4E3; --fail:#B63A43; --fail-l:#FDECEE; }
* { box-sizing: border-box; }
body { font: 15px/1.5 Inter, system-ui, "Segoe UI", Roboto, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }
.wrap { max-width: 960px; margin: 0 auto; padding: 0 20px 40px; }
header { background: linear-gradient(135deg, #12355B, #0F9D9A); color: #fff; padding: 22px 0 26px; margin-bottom: 22px; }
header .brand { font-size: 13px; letter-spacing: .12em; text-transform: uppercase; opacity: .85; }
header h1 { font-size: 26px; margin: 4px 0 6px; line-height: 1.2; }
header .sub { font-size: 13px; opacity: .85; }
.pills { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.pill { display: inline-block; font-size: 12px; font-weight: 700; border-radius: 999px; padding: 3px 10px; background: rgba(255,255,255,.16);
        border: 1px solid rgba(255,255,255,.4); color: #fff; }
.summary { background: #fff; border: 1px solid var(--line); border-left: 5px solid var(--teal); border-radius: 14px; padding: 16px 20px; }
.summary h2 { margin: 0 0 8px; font-size: 17px; }
.summary ol { margin: 0; padding-left: 20px; } .summary li { margin: 6px 0; }
section.card { background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 16px 20px; margin-top: 16px; }
h2 { font-size: 17px; color: var(--navy); margin: 0 0 4px; } .lead { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.muted { color: var(--muted); } .small { font-size: 12px; }
.note { background: var(--teal-l); border-radius: 10px; padding: 10px 12px; font-size: 13px; margin-top: 14px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { border-bottom: 1px solid var(--line); padding: 7px 8px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; font-weight: 700; }
td.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
.kv div { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; }
.kv .k { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.kv .v { font-size: 17px; font-weight: 700; color: var(--navy); }
.st { display: inline-block; font-size: 11px; font-weight: 800; border-radius: 999px; padding: 2px 9px; }
.PASS, .ok { color: var(--pass); } .st.PASS { background: var(--pass-l); } .WARN { color: var(--warn); } .st.WARN { background: var(--warn-l); }
.FAIL, .BLOCK, .broken { color: var(--fail); } .st.FAIL, .st.BLOCK { background: var(--fail-l); }
.tag { font-size: 10px; font-weight: 800; letter-spacing: .05em; border: 1px solid var(--line); border-radius: 5px; padding: 0 5px; color: var(--muted); }
.tag.REAL { color: var(--pass); border-color: #9fd8bf; } .tag.PROXY { color: #1B4A78; border-color: #b8cce0; } .tag.SYNTH { border-style: dashed; }
.big { font-size: 22px; font-weight: 800; color: var(--navy); margin: 0 0 8px; }
.chain { display: flex; gap: 12px; align-items: center; padding: 12px 14px; border-radius: 12px; font-weight: 700; }
.chain.ok { background: var(--pass-l); } .chain.broken { background: var(--fail-l); }
details { margin-top: 18px; background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 12px 18px; }
details summary { cursor: pointer; font-weight: 700; color: var(--navy); }
details h3, section h3 { font-size: 14px; margin: 16px 0 6px; }
code, pre { font: 12px ui-monospace, Consolas, monospace; } pre { white-space: pre-wrap; word-break: break-all; background: var(--bg); padding: 8px; border-radius: 8px; }
footer { margin-top: 20px; font-size: 12px; color: var(--muted); }
.pdf { margin-top: 14px; font: 700 14px Inter, system-ui, sans-serif; color: var(--navy); background: #fff; border: 0; border-radius: 999px;
       padding: 8px 16px; cursor: pointer; box-shadow: 0 6px 16px rgba(0,0,0,.2); }
.pdf:hover { background: var(--teal-l); }
@media print { body { background: #fff; } header { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .pdf { display: none; } section.card, .summary { break-inside: avoid; } }
"""

# Plain-language labels (the same words as the FlowForge UI).
CAUSE = {
    "CEILING_BELOW_COST": "it costs more to make than the price cap allows",
    "HEADROOM_ERODING": "the cost of making it is climbing towards the price cap",
    "SINGLE_ORIGIN_API": "its key ingredient comes mostly from one country",
    "FEW_PRODUCERS": "few makers are on record",
    "QUALITY_NSQ": "batches recently failed government quality tests (CDSCO)",
    "SUPPLIER_OTD_DROP": "our supplier's deliveries are slipping",
}
BAND = {"RED": "Act now", "AMBER": "Watch closely", "GREEN": "Stable"}
OPTION = {"BUFFER": "Buy extra stock from the current supplier", "ALT_SUPPLIER": "Buy from another maker",
          "THERAPEUTIC_ALT": "Switch to an alternative medicine (needs clinical sign-off)",
          "REBALANCE": "Move stock between our pharmacy locations"}
OVERALL = {"READY": "passed every rule check", "NEEDS_CHANGES": "has warnings to review", "BLOCKED": "is blocked by the rules"}
CHECK = {
    "PRICE_COMPLIANCE": "Price is within the government cap plus GST (DPCO para 14)",
    "SCHEDULED_STATUS": "Medicine is on the price-control list and its order is on record",
    "SUPPLIER_LICENCE": "Supplier has a valid drug licence and GST number and is not blocked",
    "QUALITY_HISTORY": "No recent quality failures for this supplier / maker",
    "SHELF_LIFE": "Enough shelf life left on delivery", "EXPIRY_WASTE": "Little or nothing will expire before it is used",
    "BUDGET": "Fits the department budget (large orders need a second approver)", "STORAGE": "Fits storage and cold-chain capacity",
    "ROL_ROQ": "Resulting stock stays within the reorder limits", "CLINICAL": "Clinical sign-off for a substitute medicine",
}
CHECKLIST = {
    "cp01_material_master": "The material matches: generic name, strength and pack (CP-01)",
    "cp02_rol_roq_stock": "Reorder level, quantity and the resulting stock are acceptable (CP-02)",
    "cp04_supplier_verified": "The supplier is verified: licence and past performance (CP-04)",
    "price_within_ceiling_gst": "The price is at or below the NPPA ceiling plus GST",
    "shelf_life_fefo_ok": "Shelf life and first-expiry-first-out use are acceptable",
    "cause_reviewed_not_claim": "The cause was read as a risk flag, not a claim about any manufacturer",
}
ACTION = {"APPROVE": "Approved", "EDIT_APPROVE": "Edited and approved", "REJECT": "Rejected", "SNOOZE": "Snoozed"}
# fact key -> (plain label, how to format the value)
FACT = {
    "as_of": ("Data as of", "text"), "headroom_pct": ("Margin left (what the maker keeps per unit)", "pct"),
    "headroom_trend_per_month": ("Margin trend", "spct"), "months_to_breach": ("Margin runs out in", "months"),
    "ceiling_price_inr": ("Price ceiling, excl. GST (maximum allowed price)", "inr"),
    "realisation_inr": ("What the maker earns per unit", "inr"), "estimated_unit_cost_inr": ("Estimated cost to make one unit", "inr"),
    "n_producers_min": ("Producers on public record", "atleast"),
    "producer_hhi": ("Market concentration among makers (0 = spread out, 1 = one maker)", "num"),
    "top_origin_country": ("Main country the ingredient comes from", "text"),
    "top_origin_share": ("Share of the ingredient from that country", "pct"),
    "concentration": ("Overall supply concentration", "pct"), "nsq_alerts_recent": ("Recent CDSCO quality alerts", "int"),
    "otd_drop_pts": ("Drop in our supplier's on-time deliveries", "pts"), "hospital_materials": ("Hospital material codes", "text"),
    "ved": ("Hospital criticality (Vital / Essential / Desirable)", "ved"), "nlem_level": ("Essential-medicine list: level of care", "nlem"),
    "criticality": ("Criticality weight used in the score", "num"), "usable_stock_qty": ("Usable stock in the hospital", "units"),
    "daily_consumption": ("Forecast daily use", "units"), "consumption_method": ("Forecast method", "text"),
    "consumption_months_history": ("Months of usage history", "int"), "days_of_cover": ("Days our stock lasts", "days"),
    "lead_time_days": ("Supplier lead time", "days"), "requal_days": ("Days to qualify a new supplier", "days"),
    "exit_risk": ("Exit-risk score (a flag, not a prediction)", "pct"), "exposure": ("Hospital exposure score", "num"),
    "window_months_lo": ("Exit-risk window from", "months"), "window_months_hi": ("Exit-risk window to", "months"),
}
KEY_FACTS = {"headroom_pct", "headroom_trend_per_month", "months_to_breach", "ceiling_price_inr", "realisation_inr",
             "estimated_unit_cost_inr", "n_producers_min", "top_origin_country", "top_origin_share", "nsq_alerts_recent",
             "days_of_cover", "daily_consumption", "exit_risk"}
SOURCE = {"FF_REF_CEILING_PRICE": "NPPA ceiling-price orders", "FF_REF_FORMULATION": "NPPA / NLEM medicine list",
          "FF_REF_NSQ_ALERT": "CDSCO quality alerts (NSQ)", "FF_REF_PRODUCER": "Producers from public records",
          "FF_REF_MANUFACTURER": "Manufacturers", "FF_REF_API_COST_MONTHLY": "Imported ingredient cost",
          "FF_REF_API_ORIGIN": "Ingredient origin countries", "FF_REF_BOM_ASSUMPTION": "Recipe assumptions (how one unit is made)",
          "FF_REF_WPI": "Wholesale Price Index (inflation)", "FF_REF_API": "Active ingredients",
          "FF_REF_FORM_API": "Medicine-to-ingredient map", "FF_MM_EKPO": "Hospital purchase orders",
          "FF_MM_EKKO": "Hospital purchase orders", "FF_MM_LFA1": "Hospital supplier master", "FF_MM_MARA": "Hospital material master",
          "FF_MM_MCHB": "Hospital batch stock", "FF_MM_MSEG": "Hospital stock movements", "FF_MM_T001W": "Hospital plants",
          "FF_MM_COLDCHAIN": "Cold-chain temperature logs"}


def _e(v: Any) -> str:
    v = a6._nz(v)
    return html.escape("" if v is None else str(v))


def _json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _one(db: Db, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
    df = try_query(db, sql, params)
    return df.iloc[0].to_dict() if df is not None and len(df) else None


def _table(rows: list[dict[str, Any]], cols: Sequence[str], cls_col: str | None = None) -> str:
    if not rows:
        return '<p class="muted">none</p>'
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            cls = f' class="{_e(v)}"' if c == cls_col else ""
            cells.append(f"<td{cls}>{_e(v) if not isinstance(v, (dict, list)) else '<code>' + _e(json.dumps(v)) + '</code>'}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


def gather(db: Db, rec_id: str) -> dict[str, Any]:
    """Everything the report shows, read from the FF_AG_* tables."""
    run_id, scen = a6.split_rec_id(rec_id)
    rec = a6.load_recommendation(db, rec_id)
    if rec is None:
        raise KeyError(f"unknown recommendation {rec_id}")
    form = rec["FORM_ID"]
    audit_rows = a6.DbAuditLog(db).load(run_id)
    checks = try_query(db, "SELECT CHECK_ID, NAME, STATUS, EVIDENCE_JSON, RULE_SOURCE FROM FF_AG_CHECK "
                           "WHERE RUN_ID = ? AND SCENARIO_ID = ? ORDER BY CHECK_ID", (run_id, scen))
    return {
        "rec_id": rec_id, "run_id": run_id, "rec": rec,
        "run": _one(db, "SELECT RUN_ID, STARTED_AT, FINISHED_AT, TRIGGER_TYPE, STATUS, AS_OF_DATE, SHOCK_JSON, "
                        "RULE_VERSIONS_JSON FROM FF_AG_RUN WHERE RUN_ID = ?", (run_id,)),
        "formulation": _one(db, "SELECT FORM_ID, GENERIC, STRENGTH, DOSAGE_FORM, NLEM_LEVEL, IS_PROXY "
                                "FROM FF_REF_FORMULATION WHERE FORM_ID = ?", (form,)),
        "signal": _one(db, "SELECT * FROM FF_AG_SIGNAL WHERE RUN_ID = ? AND FORM_ID = ?", (run_id, form)),
        "dependency": _one(db, "SELECT * FROM FF_AG_DEPENDENCY WHERE RUN_ID = ? AND FORM_ID = ?", (run_id, form)),
        "forecast": _one(db, "SELECT EXIT_RISK_BAND, EXIT_RISK, WINDOW_MONTHS_LO, WINDOW_MONTHS_HI, DAYS_OF_COVER, "
                             "CAUSE_CODE, CAUSE_FACTS_JSON FROM FF_AG_FORECAST WHERE RUN_ID = ? AND FORM_ID = ?",
                         (run_id, form)),
        "scenario": _one(db, "SELECT OPTION_TYPE, SUPPLIER, MATERIAL, QTY, UNIT_RATE_INR, COST_INR, COVERAGE_DAYS, "
                             "EXPIRY_WASTE_RISK, CORRELATED_RISK_FLAG FROM FF_AG_SCENARIO WHERE RUN_ID = ? "
                             "AND SCENARIO_ID = ?", (run_id, scen)),
        "checks": checks.to_dict(orient="records") if checks is not None else [],
        "approvals": a6.load_approvals(db, rec_id),
        "action": a6.load_action(db, rec_id),
        "audit": audit_rows,
        "chain": a6.verify_rows(audit_rows) if audit_rows else a6.ChainCheck(False, 0, 0, "no audit rows"),
    }


def _facts(cause_facts: Any) -> list[dict[str, Any]]:
    facts = _json(cause_facts) or {}
    rows = []
    for k, v in (facts.items() if isinstance(facts, dict) else []):
        if isinstance(v, dict):
            rows.append({"fact": k, "value": v.get("value"), "unit": v.get("unit"), "tag": v.get("tag"),
                         "source": v.get("source")})
        else:
            rows.append({"fact": k, "value": v})
    return rows


def _num(v: Any) -> float | None:
    v = a6._nz(v)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Any, kind: str) -> str:
    """One fact value in plain units; never invents a value (a missing one says so)."""
    v = a6._nz(v)
    if v is None:
        return "not on the current trend" if kind == "months" else "not available"
    n = _num(v)
    if n is not None:
        if kind == "pct":
            return f"{n * 100:.1f}%"
        if kind == "spct":
            return f"{'shrinking' if n < 0 else 'growing'} {abs(n) * 100:.1f}% per month"
        if kind == "inr":
            return f"₹{n:,.2f} per unit"
        if kind == "months":
            return f"{n:,.1f} months"
        if kind in ("days", "units", "pts"):
            return f"{n:,.0f} " + {"days": "days", "units": "units", "pts": "percentage points"}[kind]
        if kind == "int":
            return f"{n:,.0f}"
        if kind == "atleast":
            return f"at least {n:,.0f}"
        if kind == "num":
            return f"{n:.2f}"
    if kind == "nlem":
        return ", ".join({"P": "primary", "S": "secondary", "T": "tertiary"}.get(x.strip(), x.strip()) for x in str(v).split(","))
    if kind == "ved":
        return {"V": "Vital", "E": "Essential", "D": "Desirable"}.get(str(v), str(v))
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _tag(t: Any) -> str:
    t = a6._nz(t)
    return f'<span class="tag {_e(t)}">{_e(t)}</span>' if t else ""


def _money(v: Any) -> str:
    n = _num(v)
    return "not available" if n is None else f"₹{n:,.0f}"


def _source_label(src: str) -> str:
    table = str(src).split(":", 1)[0]
    return SOURCE.get(table, table.replace("FF_", "").replace("_", " ").title())


def render(data: dict[str, Any]) -> str:
    rec, fc, sc, f = data["rec"], data["forecast"] or {}, data["scenario"] or {}, data["formulation"] or {}
    name = " ".join(str(x) for x in (f.get("GENERIC"), f.get("STRENGTH"), f.get("DOSAGE_FORM")) if a6._nz(x)) or rec["FORM_ID"]
    title = f"FlowForge decision record · {rec['FORM_ID']}"
    band = a6._nz(fc.get("EXIT_RISK_BAND"))
    band_txt = BAND.get(str(band), str(band or "not assessed"))
    cause = CAUSE.get(str(fc.get("CAUSE_CODE")), str(fc.get("CAUSE_CODE") or "not recorded").lower().replace("_", " "))
    option = OPTION.get(str(sc.get("OPTION_TYPE")), str(sc.get("OPTION_TYPE") or "–"))
    overall = OVERALL.get(str(rec["OVERALL"]), str(rec["OVERALL"]).lower())
    chain, act, approvals = data["chain"], data["action"], data["approvals"]
    latest = approvals[-1] if approvals else None
    decision = ACTION.get(str(latest["ACTION"]), str(latest["ACTION"])) if latest else "waiting for a person"

    # ---- the plain summary: what, recommendation, who, what happened, trust
    qty, days, cost = _num(sc.get("QTY")), _num(sc.get("COVERAGE_DAYS")), _num(sc.get("COST_INR"))
    what = (f"FlowForge flagged <b>{_e(name)}</b> as <b>{_e(band_txt)}</b> because {_e(cause)}. "
            f"Our hospital stock lasts about <b>{_e(_fmt(fc.get('DAYS_OF_COVER'), 'days'))}</b>.")
    rec_line = (f"<b>{_e(option)}</b>" + (f", {qty:,.0f} units" if qty is not None else "")
                + (f" for {_money(cost)}" if cost and sc.get("OPTION_TYPE") != "REBALANCE" else "")
                + (f", covering about {days:,.0f} days" if days is not None else "") + f". The option {_e(overall)} (agent A5).")
    if latest:
        who = (f"<b>{_e(decision)}</b> by <b>{_e(latest['APPROVER'])}</b> (level {_e(latest['LEVEL_NO'])}) "
               f"on {_e(latest['DECIDED_AT'])} UTC.")
    else:
        who = "<b>No one has decided yet</b>: it is waiting for the Chief Pharmacist. Nothing is bought without a person."
    did = (f"Purchase requisition <b>{_e(act['BANFN'])}</b> was created in SAP S/4HANA format "
           f"({_e(act['S4_SERVICE'])}; a mock posting, not a live system) on {_e(act['POSTED_AT'])} UTC."
           if act else "No purchase requisition was created.")
    trust = (f"The audit chain is <b class='ok'>intact</b>: {chain.n_rows} linked records, none altered."
             if chain.ok else f"The audit chain is <b class='broken'>broken</b>: {_e(str(chain))}.")

    # ---- 1. facts in plain words
    facts = _json(fc.get("CAUSE_FACTS_JSON")) or {}
    fact_rows, more_rows = [], []
    for k, (label, kind) in FACT.items():
        v = facts.get(k) if isinstance(facts, dict) else None
        if isinstance(v, dict) and (a6._nz(v.get("value")) is not None or k == "months_to_breach"):
            (fact_rows if k in KEY_FACTS else more_rows).append(
                f"<tr><td>{_e(label)}</td><td class='num'>{_e(_fmt(v.get('value'), kind))}</td><td>{_tag(v.get('tag'))}</td></tr>")

    # ---- 3. rule checks
    checks = data["checks"]
    n_pass = sum(1 for c in checks if c["STATUS"] == "PASS")
    check_rows = "".join(f"<tr><td><span class='st {_e(c['STATUS'])}'>{_e(c['STATUS'])}</span></td>"
                         f"<td>{_e(CHECK.get(c['CHECK_ID'], c['NAME']))}<div class='small muted'>{_e(c['RULE_SOURCE'])}</div></td></tr>"
                         for c in checks)

    # ---- 4. human decision
    appr_rows = "".join(f"<tr><td>{_e(a['LEVEL_NO'])}</td><td>{_e(a['APPROVER'])}</td><td>{_e(ACTION.get(str(a['ACTION']), a['ACTION']))}</td>"
                        f"<td>{_e(a['DECIDED_AT'])}</td><td>{_e(a6._nz(a['REASON']) or '')}</td></tr>" for a in approvals)
    cl = (_json(latest["CHECKLIST_JSON"]) or {}) if latest else {}
    cl_rows = "".join(f"<tr><td class='{'ok' if v else 'broken'}' style='white-space:nowrap'>{'✓ ticked' if v else '✗ not ticked'}</td>"
                      f"<td>{_e(CHECKLIST.get(k, k))}</td></tr>" for k, v in (cl.items() if isinstance(cl, dict) else []))
    appr_html = (f"<table><tr><th>Level</th><th>Who</th><th>Decision</th><th>When (UTC)</th><th>Reason</th></tr>{appr_rows}</table>"
                 if appr_rows else '<p class="muted">No one has decided yet.</p>')
    cl_html = f"<h3>Checklist at the latest decision</h3><table>{cl_rows}</table>" if cl_rows else ""

    # ---- 6. sources, grouped by what they are
    sources: dict[str, dict[str, Any]] = {}
    for r in data["audit"]:
        for s_ in _json(r["DATA_SOURCES_JSON"]) or []:
            if isinstance(s_, dict) and "source" in s_:
                sources.setdefault(s_["source"], {"source": s_["source"], "tag": s_.get("is_proxy"), "fetched_at": s_.get("fetched_at"),
                                                  "url": s_.get("source_url"), "used by": set()})["used by"].add(r["AGENT"])
    friendly: dict[tuple[str, str], set[str]] = {}
    for v in sources.values():
        friendly.setdefault((_source_label(v["source"]), str(v["tag"])), set()).update(v["used by"])
    src_rows = "".join(f"<tr><td>{_e(lbl)}</td><td>{_tag(t)}</td><td>{_e(', '.join(sorted(ag)))}</td></tr>"
                       for (lbl, t), ag in sorted(friendly.items()))
    counts = {t: sum(1 for (_, tt) in friendly if tt == t) for t in ("REAL", "PROXY", "SYNTH")}

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    no_facts = '<tr><td colspan="3" class="muted">No facts recorded.</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head><body>
<header><div class="wrap">
<div class="brand">FlowForge · NABH evidence report</div>
<h1>Decision record: {_e(name)}</h1>
<div class="sub">Generated {generated} · recommendation <code>{_e(data['rec_id'])}</code></div>
<div class="pills"><span class="pill">Shortage risk: {_e(band_txt)}</span><span class="pill">Decision: {_e(decision)}</span>
<span class="pill">Audit chain: {'intact' if chain.ok else 'BROKEN'}</span></div>
<button class="pdf" onclick="window.print()">&#8595; Download as PDF</button>
</div></header>
<div class="wrap">
<div class="summary"><h2>In plain words</h2><ol>
<li><b>What:</b> {what}</li><li><b>Recommendation:</b> {rec_line}</li><li><b>Who decided:</b> {who}</li>
<li><b>What happened:</b> {did}</li><li><b>Can it be trusted:</b> {trust}</li></ol>
<p class="note">{_e(DISCLAIMER)}</p></div>

<section class="card"><h2>1 · Why the medicine was flagged</h2>
<p class="lead">The facts the rules used. Each shows where it came from: REAL = public government data, PROXY = an estimate from public data, SYNTH = demo data.</p>
<table><tr><th>Fact</th><th>Value</th><th>Source</th></tr>{''.join(fact_rows) or no_facts}</table>
{f"<details style='border:0;padding:0;margin-top:10px'><summary>Show all {len(more_rows)} other facts</summary><table>{''.join(more_rows)}</table></details>" if more_rows else ""}</section>

<section class="card"><h2>2 · What was recommended</h2>
<div class="kv"><div><div class="k">Action</div><div class="v" style="font-size:15px">{_e(option)}</div></div>
<div><div class="k">Quantity</div><div class="v">{_e(_fmt(sc.get('QTY'), 'int'))}</div></div>
<div><div class="k">Cost</div><div class="v">{_money(cost)}</div></div>
<div><div class="k">Covers</div><div class="v">{_e(_fmt(sc.get('COVERAGE_DAYS'), 'days'))}</div></div>
<div><div class="k">Supplier</div><div class="v" style="font-size:15px">{_e(a6._nz(sc.get('SUPPLIER')) or 'none (internal move)')}</div></div></div></section>

<section class="card"><h2>3 · Automatic rule checks</h2>
<p class="lead">Agent A5 checks the price law (DPCO) and the hospital's SOP before anyone sees the option.</p>
<p class="big">{n_pass} of {len(checks)} passed</p>
<table><tr><th>Result</th><th>Check</th></tr>{check_rows}</table></section>

<section class="card"><h2>4 · The human decision</h2>
<p class="lead">No purchase happens without a named person ticking the checklist.</p>
{appr_html}{cl_html}</section>

<section class="card"><h2>5 · What happened</h2><p>{did}</p></section>

<section class="card"><h2>6 · Evidence and integrity</h2>
<div class="chain {'ok' if chain.ok else 'broken'}">{'✓' if chain.ok else '✗'} {trust}</div>
<p class="small muted">Verification: {_e(str(chain))}.</p>
<p class="lead" style="margin-top:12px">{len(friendly)} data sources: {counts['REAL']} REAL, {counts['PROXY']} PROXY, {counts['SYNTH']} SYNTH.</p>
<table><tr><th>Source</th><th>Type</th><th>Used by agent</th></tr>{src_rows}</table></section>

<footer>FlowForge · SAP Hackfest 2026 · use "Download as PDF" (Save as PDF) to file this in the NABH record.</footer>
</div></body></html>
"""


def write_report(db: Db, rec_id: str, out_dir: str | Path = "reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"decision_{rec_id.replace(':', '_')}.html"
    path.write_text(render(gather(db, rec_id)), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agents.report", description=__doc__.split("\n")[0])
    p.add_argument("--rec", required=True, help="recommendation id <run_id>:<form_id>:<nn>")
    p.add_argument("--out", default="reports", help="output directory")
    a = p.parse_args(argv)
    with get_db() as db:
        path = write_report(db, a.rec, a.out)
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
