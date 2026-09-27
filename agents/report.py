"""Per-decision audit report as HTML for NABH evidence (plan §3 A6 reports): who, what, why, sources, hashes.

    python -m agents.report --rec <run_id>:<form_id>:<nn> [--out reports/]

Self-contained HTML (inline CSS, no scripts) so it prints to PDF from any browser.
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
:root { --fg:#1b1f24; --muted:#5b6470; --line:#d9dee4; --bg:#ffffff; --pass:#1a7f37; --warn:#9a6700; --fail:#cf222e; }
body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg); background: var(--bg);
       max-width: 980px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 24px 0 8px; border-bottom: 1px solid var(--line); }
.muted { color: var(--muted); } .note { background:#f6f8fa; border:1px solid var(--line); padding:8px 10px; }
table { border-collapse: collapse; width: 100%; } th, td { border: 1px solid var(--line); padding: 4px 6px;
       text-align: left; vertical-align: top; } th { background: #f6f8fa; font-weight: 600; }
code, pre { font: 12px ui-monospace, Consolas, monospace; } pre { white-space: pre-wrap; word-break: break-all; }
.PASS, .ok { color: var(--pass); font-weight: 600; } .WARN { color: var(--warn); font-weight: 600; }
.FAIL, .broken { color: var(--fail); font-weight: 600; }
.tag { font-size: 11px; border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; }
@media print { body { margin: 0; } }
"""


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


def render(data: dict[str, Any]) -> str:
    rec, fc, sc, f = data["rec"], data["forecast"] or {}, data["scenario"] or {}, data["formulation"] or {}
    title = f"FlowForge decision record · {rec['FORM_ID']}"
    approvals = []
    for a in data["approvals"]:
        cl = _json(a["CHECKLIST_JSON"]) or {}
        ticked = sum(bool(v) for v in cl.values()) if isinstance(cl, dict) else 0
        approvals.append({"level": a["LEVEL_NO"], "approver": a["APPROVER"], "action": a["ACTION"],
                          "decided_at (UTC)": a["DECIDED_AT"], "checklist": f"{ticked}/{len(cl)} ticked",
                          "reason": a["REASON"], "edited qty": a["EDITED_QTY"], "edited supplier": a["EDITED_SUPPLIER"]})
    checklist_rows = []
    if data["approvals"]:
        cl = _json(data["approvals"][-1]["CHECKLIST_JSON"]) or {}
        checklist_rows = [{"item": k, "ticked": "yes" if v else "NO"} for k, v in cl.items()]
    sources: dict[str, dict[str, Any]] = {}
    for r in data["audit"]:
        for s in _json(r["DATA_SOURCES_JSON"]) or []:
            if isinstance(s, dict) and "source" in s:
                sources.setdefault(s["source"], {"source": s["source"], "tag": s.get("is_proxy"),
                                                 "fetched_at": s.get("fetched_at"), "url": s.get("source_url"),
                                                 "used by": set()})["used by"].add(r["AGENT"])
    src_rows = [{**v, "used by": ", ".join(sorted(v["used by"]))} for v in sorted(sources.values(), key=lambda x: x["source"])]
    audit_rows = [{"seq": r["SEQ"], "agent": r["AGENT"], "ts (UTC)": r["TS"], "input_hash": r["INPUT_HASH"][:16],
                   "output_hash": r["OUTPUT_HASH"][:16], "prev_hash": (a6._nz(r["PREV_HASH"]) or "-")[:16],
                   "row_hash": r["ROW_HASH"]} for r in data["audit"]]
    rv = (_json(data["audit"][0]["RULE_VERSIONS_JSON"]) if data["audit"] else None) or {}
    chain = data["chain"]
    act = data["action"]
    act_html = '<p class="muted">No purchase requisition posted.</p>'
    if act:
        act_html = (f"<p>PR <b>{_e(act['BANFN'])}</b> posted {_e(act['POSTED_AT'])} UTC to the FF_MM_EBAN mock, "
                    f"shaped on S/4HANA <code>{_e(act['S4_SERVICE'])}</code>. Payload sha256 "
                    f"<code>{_e(act['PAYLOAD_HASH'])}</code>.</p>"
                    f"<pre>{_e(json.dumps(_json(act['PAYLOAD_JSON']), indent=2))}</pre>"
                    f"<p>API Business Hub master-data check (read-only):</p>"
                    + _table(_json(act["MASTERDATA_JSON"]) or [], ("service", "key", "status", "note"), "status"))
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head><body>
<h1>{_e(title)}</h1>
<p class="muted">Recommendation <code>{_e(data['rec_id'])}</code> · run <code>{_e(data['run_id'])}</code> · generated {generated}</p>
<p class="note">{_e(DISCLAIMER)}</p>

<h2>What</h2>
<p>{_e(f.get('GENERIC', rec['FORM_ID']))} {_e(f.get('STRENGTH', ''))} {_e(f.get('DOSAGE_FORM', ''))}
 <span class="tag">{_e(f.get('IS_PROXY', ''))}</span> — option <b>{_e(sc.get('OPTION_TYPE'))}</b>,
 supplier {_e(sc.get('SUPPLIER'))}, material {_e(sc.get('MATERIAL'))}, qty {_e(sc.get('QTY'))},
 cost INR {_e(sc.get('COST_INR'))}, coverage {_e(sc.get('COVERAGE_DAYS'))} days. A5 verdict: <b>{_e(rec['OVERALL'])}</b>.</p>

<h2>Why</h2>
<p>Exit risk <b>{_e(fc.get('EXIT_RISK_BAND'))}</b>, stated cause <b>{_e(fc.get('CAUSE_CODE'))}</b>,
 window {_e(fc.get('WINDOW_MONTHS_LO'))}–{_e(fc.get('WINDOW_MONTHS_HI'))} months, hospital cover {_e(fc.get('DAYS_OF_COVER'))} days.</p>
{_table(_facts(fc.get('CAUSE_FACTS_JSON')), ('fact', 'value', 'unit', 'tag', 'source'))}
<h2>Policy checks (A5)</h2>
{_table(data['checks'], ('CHECK_ID', 'NAME', 'STATUS', 'RULE_SOURCE'), 'STATUS')}

<h2>Who</h2>
{_table(approvals, ('level', 'approver', 'action', 'decided_at (UTC)', 'checklist', 'reason', 'edited qty', 'edited supplier'))}
<p>Manual verification checklist (latest decision):</p>
{_table(checklist_rows, ('item', 'ticked'))}

<h2>Action</h2>
{act_html}

<h2>Sources</h2>
{_table(src_rows, ('source', 'tag', 'fetched_at', 'url', 'used by'))}
<p>Rule files: {', '.join(f'<code>{_e(k)} {_e(v)}</code>' for k, v in sorted(rv.items())) or 'n/a'}</p>

<h2>Hashes (FF_AG_AUDIT_LOG)</h2>
<p class="{'ok' if chain.ok else 'broken'}">{_e(str(chain))}</p>
{_table(audit_rows, ('seq', 'agent', 'ts (UTC)', 'input_hash', 'output_hash', 'prev_hash', 'row_hash'))}
</body></html>
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
