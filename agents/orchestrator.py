"""Orchestrator: sequences A1 → A5, audits every hop through A6, and stops at the human gate (plan §2, §3).

    run(form_ids, shock)  → FF_AG_RUN status AWAITING_APPROVAL (or NO_ACTION when nothing reaches the gate)
    decide(rec_id, …)     → FF_AG_APPROVAL row; on an approval that needs nothing more, A6 acts

No agent calls another; only this module passes outputs along. Every output is a probabilistic
flag for review. A dry run writes nothing to the DB: agents skip their tables and the audit chain
is kept (and verified) in memory.

CLI: python -m agents.orchestrator --molecule F001 --shock 1.3 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from pydantic import ValidationError

from agents import a1_margin_sentinel as a1
from agents import a2_dependency as a2
from agents import a3_forecast as a3
from agents import a4_resilience as a4
from agents import a5_validator as a5
from agents import a6_audit as a6
from agents.common import AgentCtx, load_fixture_db, placeholders, try_query
from agents.contracts import (
    ApprovalAction, ApprovalChecklist, ApprovalDecision, DependencyProfile, Forecast, Recommendation, RiskSignal,
    RunContext, RunTrigger, Scenario, ShockSpec,
)
from agents.ctx import SQLITE_PATH, Db, get_db, load_settings
from agents.rules import Rules, load_rules

log = logging.getLogger(__name__)

AWAITING_APPROVAL = "AWAITING_APPROVAL"
RUN_COLS = ("RUN_ID", "STARTED_AT", "FINISHED_AT", "TRIGGER_TYPE", "STATUS", "FORMULATION_IDS_JSON", "AS_OF_DATE",
            "SHOCK_JSON", "DRY_RUN", "RULE_VERSIONS_JSON")
APPROVAL_COLS = ("RUN_ID", "SCENARIO_ID", "LEVEL_NO", "DECIDED_AT", "APPROVER", "ACTION", "CHECKLIST_JSON", "REASON",
                 "EDITED_QTY", "EDITED_SUPPLIER", "SNOOZE_DAYS")
_ACTION_ALIASES = {"APPROVED": "APPROVE", "REJECTED": "REJECT", "SNOOZED": "SNOOZE", "EDIT": "EDIT_APPROVE"}


class DecisionRefused(ValueError):
    """The human-gate decision is invalid (e.g. a REJECT without a reason) and was not recorded."""


# ---------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dt(db: Db, v: datetime) -> Any:
    return v.isoformat(timespec="microseconds") if db.backend == "sqlite" else v


def as_of_date(db: Db) -> date:
    """FF_CFG_PARAM.AS_OF_DATE (demo clock) if set, else today (UTC)."""
    df = try_query(db, "SELECT DATE_VALUE FROM FF_CFG_PARAM WHERE NAME = ?", ("AS_OF_DATE",))
    if df is not None and len(df) and df.iloc[0]["DATE_VALUE"]:
        v = df.iloc[0]["DATE_VALUE"]
        return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
    return datetime.now(timezone.utc).date()


def resolve_form_ids(db: Db, ids: Sequence[str]) -> list[str]:
    """Accept formulation ids (F001) or generic names (amoxicillin → every matching formulation)."""
    out: list[str] = []
    for x in ids:
        df = db.query("SELECT FORM_ID FROM FF_REF_FORMULATION WHERE FORM_ID = ? OR LOWER(GENERIC) = LOWER(?) "
                      "ORDER BY FORM_ID", (x, x))
        found = df["FORM_ID"].astype(str).tolist()
        if not found:
            raise ValueError(f"unknown formulation / molecule: {x!r}")
        out += [f for f in found if f not in out]
    return out


def _write_run(db: Db, rc: RunContext, status: str, finished: datetime | None) -> None:
    db.execute(f"INSERT INTO FF_AG_RUN ({', '.join(RUN_COLS)}) VALUES ({placeholders(RUN_COLS)})", (
        rc.run_id, _dt(db, rc.started_at.replace(tzinfo=None)), _dt(db, finished) if finished else None,
        rc.trigger.value, status, json.dumps(rc.formulation_ids), rc.as_of.isoformat() if rc.as_of else None,
        rc.shock.model_dump_json() if rc.shock else None, int(rc.dry_run), json.dumps(rc.rule_versions)))


def _set_run_status(db: Db, run_id: str, status: str) -> None:
    db.execute("UPDATE FF_AG_RUN SET STATUS = ?, FINISHED_AT = ? WHERE RUN_ID = ?", (status, _dt(db, _now()), run_id))


# ---------------------------------------------------------------- run


@dataclass
class RunResult:
    run_id: str
    status: str
    signals: list[RiskSignal]
    dependencies: list[DependencyProfile]
    forecasts: list[Forecast]
    scenarios: list[Scenario]
    recommendations: list[Recommendation]
    events: list[Any]
    chain: a6.ChainCheck
    dry_run: bool = False

    @property
    def gate(self) -> list[Recommendation]:
        return [r for r in self.recommendations if r.reaches_gate]


def run(form_ids: Sequence[str], shock: float = 1.0, *, db: Db | None = None, rules: Rules | None = None,
        dry_run: bool = False, as_of: date | None = None, run_id: str | None = None) -> RunResult:
    """A1 → A5 with an audit hop after each agent; stops at the human gate."""
    if shock <= 0:
        raise ValueError("shock must be > 0 (1.3 = API cost +30%)")
    own_db = db is None
    db = db or get_db()
    try:
        return _run(db, rules or load_rules(), list(form_ids), shock, dry_run, as_of, run_id)
    finally:
        if own_db:
            db.close()


def _run(db: Db, rules: Rules, form_ids: list[str], shock: float, dry_run: bool, as_of: date | None,
         run_id: str | None) -> RunResult:
    ids = resolve_form_ids(db, form_ids) if form_ids else []
    versions = a6.rule_file_versions(rules)
    rc = RunContext(
        run_id=run_id or f"ff-{uuid.uuid4().hex[:12]}",
        trigger=RunTrigger.SHOCK if shock != 1.0 else RunTrigger.ON_DEMAND,
        formulation_ids=ids, as_of=as_of or as_of_date(db), dry_run=dry_run, rule_versions=versions,
        shock=ShockSpec(api_cost_pct=round((shock - 1) * 100, 6), label=f"API cost x{shock:g}") if shock != 1.0 else None,
    )
    ctx = AgentCtx(db, rules, rc)
    sink: Db | a6.MemoryAuditLog = a6.MemoryAuditLog() if dry_run else db
    if not dry_run:
        _write_run(db, rc, "RUNNING", None)

    events = []

    def hop(agent: str, inp: Any, out: Any) -> None:
        srcs = a6.data_sources(db, a6.AGENT_SOURCES.get(agent, ()))
        events.append(a6.hop(sink, rc.run_id, agent, inp, out, srcs, versions))

    try:
        # Shock travels in RunContext.shock; A1's multiplier stays 1.0 so it is applied once.
        sigs = a1.run(ids, ctx)
        hop("A1", {"run": rc, "form_ids": ids}, sigs)
        deps = a2.run([s.formulation_id for s in sigs], ctx)
        hop("A2", {"run": rc, "form_ids": [s.formulation_id for s in sigs]}, deps)
        fcs = a3.run(sigs, deps, ctx)
        hop("A3", {"signals": sigs, "dependencies": deps}, fcs)
        scen = a4.run(fcs, ctx)
        hop("A4", {"forecasts": fcs}, scen)
        recs = a5.run(scen, fcs, ctx)
        hop("A5", {"scenarios": scen, "forecasts": fcs}, recs)
    except Exception:
        if not dry_run:
            _set_run_status(db, rc.run_id, "FAILED")
        raise

    status = AWAITING_APPROVAL if any(r.reaches_gate for r in recs) else "NO_ACTION"
    if not dry_run:
        _set_run_status(db, rc.run_id, status)
    chain = a6.verify_chain(rc.run_id, sink)
    return RunResult(rc.run_id, status, sigs, deps, fcs, scen, recs, events, chain, dry_run)


# ---------------------------------------------------------------- human gate


@dataclass
class DecisionResult:
    rec_id: str
    status: str  # ACTIONED | AWAITING_SECOND_APPROVER | REJECTED | SNOOZED
    decision: ApprovalDecision
    action: a6.ActResult | None = None
    notes: list[str] = field(default_factory=list)


def _action(decision: str | ApprovalAction, edits: dict[str, Any] | None) -> ApprovalAction:
    if isinstance(decision, ApprovalAction):
        a = decision
    else:
        key = str(decision).strip().upper()
        try:
            a = ApprovalAction(_ACTION_ALIASES.get(key, key))
        except ValueError:
            raise DecisionRefused(f"unknown decision {decision!r}") from None
    return ApprovalAction.EDIT_APPROVE if a is ApprovalAction.APPROVE and edits else a


def decide(rec_id: str, user: str, decision: str | ApprovalAction,
           checklist: ApprovalChecklist | dict[str, bool] | None = None, edits: dict[str, Any] | None = None,
           reason: str | None = None, *, db: Db, rules: Rules | None = None, level: int = 1,
           snooze_days: int | None = None, check_master: bool = True) -> DecisionResult:
    """Record a human decision in FF_AG_APPROVAL; on a sufficient approval, A6 posts the PR.

    `edits` = {"qty": float, "supplier": str}. A reason is required on REJECT. A second, different
    approver (level 2) is needed above the ₹ threshold or for a therapeutic substitute.
    """
    rules = rules or load_rules()
    action = _action(decision, edits)
    user = (user or "").strip()
    if not user:
        raise DecisionRefused("approver user id is required")
    if action is ApprovalAction.REJECT and not (reason and reason.strip()):
        raise DecisionRefused("a reason is required to reject")
    if level not in (1, 2):
        raise DecisionRefused("level must be 1 (Chief Pharmacist) or 2 (Procurement / Director)")

    rec = a6.load_recommendation(db, rec_id)
    if rec is None:
        raise DecisionRefused(f"unknown recommendation {rec_id}")
    if rec["OVERALL"] not in ("READY", "NEEDS_CHANGES"):
        raise DecisionRefused(f"recommendation is {rec['OVERALL']}; it does not reach the gate")
    if a6.load_action(db, rec_id) is not None:
        raise DecisionRefused(f"{rec_id} was already actioned")
    if level == 2:
        l1 = a6.latest_by_level(a6.load_approvals(db, rec_id)).get(1)
        if not l1 or l1["ACTION"] not in (ApprovalAction.APPROVE.value, ApprovalAction.EDIT_APPROVE.value):
            raise DecisionRefused("level-2 decision needs a level-1 approval first")
        if str(l1["APPROVER"]).strip().lower() == user.lower():
            raise DecisionRefused("the level-2 approver must be a different person from level 1")

    edits = edits or {}
    try:
        dec = ApprovalDecision(
            run_id=rec["RUN_ID"], scenario_id=rec["SCENARIO_ID"], approver=user, level=level, action=action,
            checklist=ApprovalChecklist.model_validate(checklist or {}), reason=reason,
            edited_qty=edits.get("qty"), edited_supplier=edits.get("supplier"), snooze_days=snooze_days)
    except ValidationError as exc:
        raise DecisionRefused("; ".join(e["msg"] for e in exc.errors())) from None

    decided = dec.decided_at.astimezone(timezone.utc).replace(tzinfo=None)
    db.execute(f"INSERT INTO FF_AG_APPROVAL ({', '.join(APPROVAL_COLS)}) VALUES ({placeholders(APPROVAL_COLS)})", (
        dec.run_id, dec.scenario_id, dec.level, _dt(db, decided), dec.approver, dec.action.value,
        dec.checklist.model_dump_json(), dec.reason, dec.edited_qty, dec.edited_supplier, dec.snooze_days))
    a6.hop(db, dec.run_id, "GATE", {"rec_id": rec_id, "recommendation": rec}, dec,
           rule_versions=a6.rule_file_versions(rules))

    if action is ApprovalAction.REJECT:
        return DecisionResult(rec_id, "REJECTED", dec)
    if action is ApprovalAction.SNOOZE:
        return DecisionResult(rec_id, "SNOOZED", dec, notes=[f"re-evaluate in {snooze_days} days"])

    latest = a6.latest_by_level(a6.load_approvals(db, rec_id))
    draft = a6.edited_draft(rec, latest) if rec.get("DRAFT_PR_JSON") else None
    if a6.needs_second_approver(rec, draft, rules) and level == 1:
        return DecisionResult(rec_id, "AWAITING_SECOND_APPROVER", dec,
                              notes=["above ₹ threshold or therapeutic substitute: level-2 approval needed"])
    if draft is None:
        return DecisionResult(rec_id, "APPROVED_NO_PR", dec, notes=["internal rebalance: no purchase requisition"])
    res = a6.act(rec_id, db, rules, check_master=check_master)
    return DecisionResult(rec_id, "ACTIONED", dec, res)


# ---------------------------------------------------------------- CLI


def open_db(fixtures: bool = False) -> tuple[Db, str]:
    """The configured DB, or an in-memory SQLite seeded with the SYNTH test fixtures."""
    s = load_settings()
    if not fixtures and (s.db_backend == "hana" or SQLITE_PATH.exists()):
        db = get_db(s)
        if db.backend == "hana" or try_query(db, "SELECT FORM_ID FROM FF_REF_CEILING_PRICE WHERE 1 = 0") is not None:
            return db, db.backend
        db.close()
    return load_fixture_db(), "sqlite:fixtures (SYNTH)"


def format_result(r: RunResult) -> str:
    lines = [f"run_id={r.run_id}  status={r.status}  dry_run={r.dry_run}  (probabilistic flags for review)", "",
             f"{'SEQ':<4}{'AGENT':<6}{'INPUT':<14}{'OUTPUT':<14}{'PREV':<14}ROW"]
    for e in r.events:
        lines.append(f"{e.seq:<4}{e.agent:<6}{e.input_hash[:12]:<14}{e.output_hash[:12]:<14}"
                     f"{(e.prev_hash or '-')[:12]:<14}{(e.event_hash or '')[:12]}")
    lines.append(f"audit: {r.chain}")
    lines.append("")
    for s in r.signals:
        dep = next((d for d in r.dependencies if d.formulation_id == s.formulation_id), None)
        fc = next((f for f in r.forecasts if f.formulation_id == s.formulation_id), None)
        lines.append(f"{s.formulation_id}: margin {s.band.value} headroom {s.headroom_pct:.1%}"
                     + (f" · at least {dep.n_producers_min} producers" if dep else "")
                     + (f" · exit risk {fc.exit_risk_band.value}, cause {fc.cause_code.value}" if fc else ""))
    lines.append("")
    by_id = {s.scenario_id: s for s in r.scenarios}
    lines.append(f"human gate: {len(r.gate)} recommendation(s) awaiting approval")
    for rec in r.recommendations:
        s = by_id[rec.scenario_id]
        flags = ",".join(f"{c.check_id}={c.status.value}" for c in rec.checks if c.status.value != "PASS") or "all PASS"
        lines.append(f"  {rec.scenario_id:<30}{rec.overall.value:<14}{s.option_type.value:<16}qty={s.qty:g} "
                     f"INR {s.cost}{' · 2nd approver' if rec.needs_second_approver else ''}  [{flags}]")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agents.orchestrator", description=__doc__.split("\n")[0])
    p.add_argument("--molecule", action="append", default=[], help="formulation id or generic name (repeatable)")
    p.add_argument("--shock", type=float, default=1.0, help="API cost multiplier, e.g. 1.3 = +30%%")
    p.add_argument("--dry-run", action="store_true", help="write nothing; audit chain kept in memory")
    p.add_argument("--fixtures", action="store_true", help="use the SYNTH test fixtures in memory")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    db, label = open_db(a.fixtures)
    print(f"[orchestrator] db={label}")
    with db:
        r = run(a.molecule, a.shock, db=db, dry_run=a.dry_run)
    print(format_result(r))
    return 0 if r.chain.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
