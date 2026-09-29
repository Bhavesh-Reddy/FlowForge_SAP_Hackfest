"""FlowForge REST API for SAP Build Apps (plan §2, §7, §8). Local: `uvicorn api.main:app`.

Every route except /health needs the header `X-API-Key: <APP_API_KEY>`. HANA credentials come from
the Cloud Foundry user-provided service `ff-hana` (VCAP_SERVICES) or from the environment.
Numbers come from the deterministic agents; every flag is a probabilistic signal for review.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator

import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import APIKeyHeader

from agents import a1_margin_sentinel as a1
from agents import a2_dependency as a2
from agents import a6_audit as a6
from agents import orchestrator as orch
from agents import report
from agents.explain import Explanation, explain
from agents.common import AgentCtx, placeholders
from agents.contracts import (
    ApprovalAction, AuditEvent, CheckResult, DataQuality, DataSourceRef, DependencyProfile, DraftPR, Forecast,
    Recommendation, RiskSignal, RunContext, Scenario, SecondarySignal,
)
from agents.ctx import REPO_ROOT, SQLITE_PATH, Db, Settings, get_db, load_settings
from agents.rules import Rules, load_rules
from api import cf
from api.models import (
    MAX_FORMS_PER_RUN, ApprovalRequest, ApprovalResponse, AuditResponse, ChainStatus, GraphEdge, GraphNode,
    GraphSnapshot, Health, MarginPoint, MarginSeries, MoleculeDetail, PendingApproval, RecallResponse,
    RecommendationView, RunRequest, RunResponse, ShockRequest, ShockResponse, ShockRow, WatchlistRow,
)

log = logging.getLogger(__name__)

VERSION = "s09"
DISCLAIMER = ("Probabilistic risk flag for review from deterministic rules; not a statement about any "
              "manufacturer's intent. Producer counts are lower bounds.")
# SAP Build Apps preview/runtime and BTP-hosted apps, plus local fallback UI (S10). Extra origins: CORS_ORIGINS.
BUILD_APPS_ORIGIN_REGEX = (r"https://([a-z0-9-]+\.)*(hana\.ondemand\.com|build\.cloud\.sap|appgyver\.com"
                           r"|appgyver\.page|cloud\.sap)|http://(localhost|127\.0\.0\.1)(:\d+)?")
PENDING_SCAN_LIMIT = 200
FALLBACK_UI = REPO_ROOT / "ui" / "fallback" / "index.html"
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False, description="APP_API_KEY")


# ---------------------------------------------------------------- plumbing


def _nz(v: Any) -> Any:
    """NULL/NaN -> None; numpy scalars -> Python."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes, Decimal)) else v


def _row(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _nz(v) for k, v in d.items()}


def _json(v: Any, default: Any) -> Any:
    v = _nz(v)
    if v in (None, ""):
        return default
    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return default


def _money(v: Any) -> Decimal | None:
    v = _nz(v)
    return None if v is None else Decimal(str(round(float(v), 4)))


def _records(db: Db, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [_row(r) for r in db.query(sql, params).to_dict(orient="records")]


def _one(db: Db, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = _records(db, sql, params)
    return rows[0] if rows else None


def require_key(request: Request, key: str | None = Security(API_KEY_HEADER)) -> None:
    expected = request.app.state.settings.app_api_key
    if not expected:
        raise HTTPException(503, "APP_API_KEY is not configured on the server")
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(401, "missing or invalid X-API-Key")


def get_conn(request: Request) -> Iterator[Db]:
    db = request.app.state.db_factory()
    try:
        yield db
    finally:
        db.close()


def rules_of(request: Request) -> Rules:
    return request.app.state.rules


def _default_factory(settings: Settings) -> Callable[[], Db]:
    def factory() -> Db:
        if settings.db_backend == "sqlite" and not SQLITE_PATH.exists():
            raise HTTPException(503, "no local database: run `python -m ingest.load_hana --backend sqlite "
                                     "--reset --seed` or set DB_BACKEND=hana")
        return get_db(settings)
    return factory


def producers_label(n: Any) -> str:
    """Producer counts are lower bounds (plan §5); zero means none on record, not none in the market."""
    n = int(_nz(n) or 0)
    return f"at least {n} producer{'s' if n != 1 else ''}" if n else "no producer on record"


def tracked_form_ids(db: Db) -> list[str]:
    """Formulations with a BOM cost assumption: the set the agents can score."""
    return db.query("SELECT DISTINCT FORM_ID FROM FF_REF_BOM_ASSUMPTION ORDER BY FORM_ID")["FORM_ID"].astype(str).tolist()


def _resolve(db: Db, ids: list[str], limit: int) -> list[str]:
    try:
        out = orch.resolve_form_ids(db, ids) if ids else tracked_form_ids(db)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    if len(out) > limit:
        raise HTTPException(422, f"{len(out)} formulations requested; the limit is {limit}")
    return out


# ---------------------------------------------------------------- row -> contract models


def signal_of(r: dict[str, Any]) -> RiskSignal:
    return RiskSignal(
        run_id=r["RUN_ID"], formulation_id=r["FORM_ID"], band=r["BAND"],
        headroom_pct=r["HEADROOM_PCT"] if r["HEADROOM_PCT"] is not None else float("nan"),
        headroom_trend=r["HEADROOM_TREND"] or 0.0, months_to_breach=r["MONTHS_TO_BREACH"],
        realisation_inr=_money(r["REALISATION_INR"]), unit_cost_inr=_money(r["UNIT_COST_INR"]),
        secondary_signals=[SecondarySignal.model_validate(x) for x in _json(r["SECONDARY_SIGNALS_JSON"], [])],
        data_quality=DataQuality(confidence=r["CONFIDENCE"] or 0.0, tags=_json(r["DATA_TAGS_JSON"], {})))


def dependency_of(r: dict[str, Any]) -> DependencyProfile:
    tags = _json(r["DATA_TAGS_JSON"], {})
    dq = DataQuality(confidence=r["CONFIDENCE"], tags=tags) if r["CONFIDENCE"] is not None else None
    return DependencyProfile(
        run_id=r["RUN_ID"], formulation_id=r["FORM_ID"], n_producers_min=int(r["N_PRODUCERS_MIN"]), hhi=r["HHI"],
        top_origin_country=r["TOP_ORIGIN_COUNTRY"], top_origin_share=r["TOP_ORIGIN_SHARE"] or 0.0,
        concentration=r["CONCENTRATION"], affected_materials=_json(r["AFFECTED_MATERIALS_JSON"], []),
        affected_wards=_json(r["AFFECTED_WARDS_JSON"], []), data_quality=dq)


def forecast_of(r: dict[str, Any]) -> Forecast:
    sod = r["STOCKOUT_DATE_IF_EXIT"]
    return Forecast(
        run_id=r["RUN_ID"], formulation_id=r["FORM_ID"], exit_risk_band=r["EXIT_RISK_BAND"], exit_risk=r["EXIT_RISK"],
        exposure=r["EXPOSURE"], window_months_lo=r["WINDOW_MONTHS_LO"], window_months_hi=r["WINDOW_MONTHS_HI"],
        days_of_cover=r["DAYS_OF_COVER"], stockout_date_if_exit=str(sod)[:10] if sod else None,
        cause_code=r["CAUSE_CODE"], cause_facts=_json(r["CAUSE_FACTS_JSON"], {}), confidence=r["CONFIDENCE"])


def scenario_of(r: dict[str, Any]) -> Scenario:
    return Scenario(
        scenario_id=r["SCENARIO_ID"], run_id=r["RUN_ID"], formulation_id=r["FORM_ID"], option_type=r["OPTION_TYPE"],
        supplier=r["SUPPLIER"], material=r["MATERIAL"], qty=float(r["QTY"]), unit_rate_inr=_money(r["UNIT_RATE_INR"]),
        cost=_money(r["COST_INR"]), coverage_days=float(r["COVERAGE_DAYS"]),
        expiry_waste_risk=r["EXPIRY_WASTE_RISK"] or 0.0, correlated_risk_flag=bool(r["CORRELATED_RISK_FLAG"] or 0),
        correlated_risk_reason=r.get("CORRELATED_RISK_REASON"),
        rank=int(r["RANK_NO"]) if r["RANK_NO"] is not None else None)


def check_of(r: dict[str, Any]) -> CheckResult:
    return CheckResult(check_id=r["CHECK_ID"], name=r["NAME"], status=r["STATUS"],
                       evidence=_json(r["EVIDENCE_JSON"], {}), rule_source=r["RULE_SOURCE"])


def recommendation_of(r: dict[str, Any], checks: list[CheckResult]) -> Recommendation:
    draft = _json(r["DRAFT_PR_JSON"], None)
    return Recommendation(run_id=r["RUN_ID"], scenario_id=r["SCENARIO_ID"], checks=checks, overall=r["OVERALL"],
                          needs_second_approver=bool(r["NEEDS_SECOND_APPROVER"] or 0),
                          draft_pr=DraftPR.model_validate(draft) if draft else None)


def _checks(db: Db, run_id: str, scenario_id: str | None = None) -> dict[str, list[CheckResult]]:
    sql = "SELECT SCENARIO_ID, CHECK_ID, NAME, STATUS, EVIDENCE_JSON, RULE_SOURCE FROM FF_AG_CHECK WHERE RUN_ID = ?"
    params: tuple = (run_id,)
    if scenario_id:
        sql, params = sql + " AND SCENARIO_ID = ?", (run_id, scenario_id)
    out: dict[str, list[CheckResult]] = {}
    for r in _records(db, sql + " ORDER BY SCENARIO_ID, CHECK_ID", params):
        out.setdefault(r["SCENARIO_ID"], []).append(check_of(r))
    return out


def audit_event_of(r: dict[str, Any]) -> AuditEvent:
    sources = [DataSourceRef.model_validate(s) for s in _json(r["DATA_SOURCES_JSON"], [])
               if isinstance(s, dict) and "source" in s]
    return AuditEvent(run_id=r["RUN_ID"], seq=int(r["SEQ"]), agent=r["AGENT"], ts=r["TS"], input_hash=r["INPUT_HASH"],
                      output_hash=r["OUTPUT_HASH"], rule_versions=_json(r["RULE_VERSIONS_JSON"], {}),
                      data_sources=sources, prev_hash=r["PREV_HASH"], event_hash=r["ROW_HASH"])


def graph_snapshot(db: Db, form_id: str) -> GraphSnapshot:
    engine = a2.pick_engine(db)
    edges = (a2.neighbourhood_hana(db, form_id) if engine == "hana"
             else a2.neighbourhood_nx(a2.load_graph(db), form_id))
    nodes: dict[str, GraphNode] = {}
    out_edges = []
    for e in edges.to_dict(orient="records"):
        e = _row(e)
        for v in (e["SRC"], e["DST"]):
            typ, _, key = str(v).partition(":")
            nodes.setdefault(v, GraphNode(id=v, type=typ, label=key or typ))
        out_edges.append(GraphEdge(src=e["SRC"], dst=e["DST"], rel=e["REL"],
                                   weight=float(e["WEIGHT"]) if e["WEIGHT"] is not None else None,
                                   is_proxy=e["IS_PROXY"]))
    return GraphSnapshot(engine=engine, nodes=list(nodes.values()), edges=out_edges)


# ---------------------------------------------------------------- routes

public = APIRouter()
secured = APIRouter(dependencies=[Depends(require_key)])


@public.get("/health", response_model=Health)
def health(request: Request) -> Health:
    return Health(db_backend=request.app.state.settings.db_backend, version=VERSION)


@public.get("/ui", include_in_schema=False)
def fallback_ui() -> FileResponse:
    """The fallback UI (S10), served same-origin so no CORS setup is needed. Static file; data needs the API key."""
    if not FALLBACK_UI.exists():
        raise HTTPException(404, "ui/fallback/index.html is not deployed")
    return FileResponse(FALLBACK_UI, media_type="text/html")


@secured.get("/watchlist", response_model=list[WatchlistRow])
def watchlist(db: Db = Depends(get_conn), limit: int = Query(100, ge=1, le=1000), assessed_only: bool = False,
              sort: str = Query("exposure", pattern="^(exposure|exit_risk)$")) -> list[WatchlistRow]:
    """Highest exposure first (A3: exit risk × criticality × cover gap); unassessed formulations last."""
    where = "WHERE w.RUN_ID IS NOT NULL " if assessed_only else ""
    key = "fc.EXPOSURE" if sort == "exposure" else "w.EXIT_RISK"
    rows = _records(db, "SELECT w.FORM_ID, w.GENERIC, w.DOSAGE_FORM, w.STRENGTH, w.NLEM_LEVEL, w.THERAPEUTIC_CLASS, "
                        "w.CEILING_PRICE, w.GST_RATE, w.CEILING_SO_NUMBER, w.PRODUCER_COUNT_MIN, w.MIN_DAYS_OF_COVER, "
                        "w.RUN_ID, w.ASSESSED_AT, w.EXIT_RISK_BAND, w.EXIT_RISK, w.CAUSE_CODE, w.CONFIDENCE, "
                        "w.MARGIN_BAND, w.HEADROOM_PCT, w.MONTHS_TO_BREACH, w.HHI, w.TOP_ORIGIN_COUNTRY, "
                        "w.TOP_ORIGIN_SHARE, w.IS_PROXY, fc.EXPOSURE, fc.WINDOW_MONTHS_LO, fc.WINDOW_MONTHS_HI "
                        "FROM FF_V_WATCHLIST w LEFT JOIN FF_AG_FORECAST fc ON fc.RUN_ID = w.RUN_ID AND fc.FORM_ID = w.FORM_ID "
                        f"{where}ORDER BY CASE WHEN {key} IS NULL THEN 1 ELSE 0 END, {key} DESC, "
                        f"w.EXIT_RISK DESC, w.FORM_ID LIMIT {int(limit)}")
    out = []
    for r in rows:
        n = int(r["PRODUCER_COUNT_MIN"] or 0)
        out.append(WatchlistRow(
            form_id=r["FORM_ID"], generic=r["GENERIC"], dosage_form=r["DOSAGE_FORM"], strength=r["STRENGTH"],
            nlem_level=r["NLEM_LEVEL"], therapeutic_class=r["THERAPEUTIC_CLASS"],
            ceiling_price_inr=float(r["CEILING_PRICE"]) if r["CEILING_PRICE"] is not None else None,
            gst_rate=r["GST_RATE"], ceiling_so_number=r["CEILING_SO_NUMBER"], producer_count_min=n,
            producers_label=producers_label(n), min_days_of_cover=r["MIN_DAYS_OF_COVER"],
            run_id=r["RUN_ID"], assessed_at=str(r["ASSESSED_AT"]) if r["ASSESSED_AT"] else None,
            exit_risk_band=r["EXIT_RISK_BAND"], exit_risk=r["EXIT_RISK"], exposure=r["EXPOSURE"],
            window_months_lo=r["WINDOW_MONTHS_LO"], window_months_hi=r["WINDOW_MONTHS_HI"], cause_code=r["CAUSE_CODE"],
            confidence=r["CONFIDENCE"], margin_band=r["MARGIN_BAND"], headroom_pct=r["HEADROOM_PCT"],
            months_to_breach=r["MONTHS_TO_BREACH"], hhi=r["HHI"], top_origin_country=r["TOP_ORIGIN_COUNTRY"],
            top_origin_share=r["TOP_ORIGIN_SHARE"], data_tag=r["IS_PROXY"]))
    return out


@secured.get("/molecule/{form_id}", response_model=MoleculeDetail)
def molecule(form_id: str, request: Request, db: Db = Depends(get_conn)) -> MoleculeDetail:
    form = _one(db, "SELECT FORM_ID, GENERIC, DOSAGE_FORM, STRENGTH, NLEM_LEVEL, THERAPEUTIC_CLASS, CEILING_PRICE, "
                    "GST_RATE, CEILING_SO_NUMBER, CEILING_PARA, PRODUCER_COUNT_MIN, MIN_DAYS_OF_COVER, IS_PROXY "
                    "FROM FF_V_WATCHLIST WHERE FORM_ID = ?", (form_id,))
    if form is None:
        raise HTTPException(404, f"unknown formulation {form_id!r}")
    form["PRODUCERS_LABEL"] = producers_label(form["PRODUCER_COUNT_MIN"])
    sig_row = _one(db, "SELECT * FROM FF_AG_SIGNAL WHERE FORM_ID = ? ORDER BY CREATED_AT DESC, RUN_ID DESC LIMIT 1",
                   (form_id,))
    tags: dict[str, str] = {"formulation": str(form["IS_PROXY"])}
    detail = MoleculeDetail(form_id=form_id, formulation=form, graph=graph_snapshot(db, form_id), data_tags=tags,
                            disclaimer=DISCLAIMER)
    if sig_row is None:
        return detail
    run_id = sig_row["RUN_ID"]
    detail.run_id, detail.signal = run_id, signal_of(sig_row)
    tags.update({k: v.value for k, v in detail.signal.data_quality.tags.items()})
    dep = _one(db, "SELECT * FROM FF_AG_DEPENDENCY WHERE RUN_ID = ? AND FORM_ID = ?", (run_id, form_id))
    if dep:
        detail.dependency = dependency_of(dep)
        if detail.dependency.data_quality:
            tags.update({k: v.value for k, v in detail.dependency.data_quality.tags.items()})
    fc = _one(db, "SELECT * FROM FF_AG_FORECAST WHERE RUN_ID = ? AND FORM_ID = ?", (run_id, form_id))
    detail.forecast = forecast_of(fc) if fc else None
    detail.scenarios = [scenario_of(r) for r in _records(
        db, "SELECT * FROM FF_AG_SCENARIO WHERE RUN_ID = ? AND FORM_ID = ? ORDER BY SCENARIO_ID", (run_id, form_id))]
    by_id = {s.scenario_id: s for s in detail.scenarios}
    checks = _checks(db, run_id)
    detail.recommendations = [
        RecommendationView(recommendation=recommendation_of(r, checks.get(r["SCENARIO_ID"], [])),
                           scenario=by_id.get(r["SCENARIO_ID"]))
        for r in _records(db, "SELECT * FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ? AND FORM_ID = ? "
                              "ORDER BY SCENARIO_ID", (run_id, form_id))]
    detail.data_tags = tags  # pydantic copied the dict at construction; publish the merged tags
    if detail.forecast is not None:
        detail.explanation = _explanation(request, detail, form)
    return detail


def _num(x: Any) -> float | None:
    x = _nz(x)
    return None if x is None or not math.isfinite(float(x)) else float(x)


@secured.get("/molecule/{form_id}/margin-series", response_model=MarginSeries)
def margin_series(form_id: str, request: Request, db: Db = Depends(get_conn),
                  months: int = Query(12, ge=3, le=36), shock: float | None = Query(None, gt=0, le=5)) -> MarginSeries:
    """Ceiling-vs-cost chart data: A1 replayed as of each month-end (no look-ahead, nothing written)."""
    form = _one(db, "SELECT CEILING_PRICE FROM FF_V_WATCHLIST WHERE FORM_ID = ?", (form_id,))
    if form is None:
        raise HTTPException(404, f"unknown formulation {form_id!r}")
    rules = rules_of(request)
    as_of = orch.as_of_date(db)
    points, tags = [], {}
    for m in pd.period_range(end=pd.Period(as_of, "M"), periods=months, freq="M"):
        ctx = AgentCtx(db, rules, RunContext(run_id=f"series-{uuid.uuid4().hex[:8]}", dry_run=True,
                                             as_of=min(m.to_timestamp(how="end").date(), as_of)))
        s = a1.run([form_id], ctx)[0]
        sh = a1.run([form_id], ctx, api_cost_multiplier=shock)[0] if shock else None
        tags = {k: v.value for k, v in s.data_quality.tags.items()}
        points.append(MarginPoint(
            month=str(m), realisation_inr=_num(s.realisation_inr), unit_cost_inr=_num(s.unit_cost_inr),
            headroom_pct=_num(s.headroom_pct), band=s.band.value,
            shocked_unit_cost_inr=_num(sh.unit_cost_inr) if sh else None,
            shocked_headroom_pct=_num(sh.headroom_pct) if sh else None))
    return MarginSeries(form_id=form_id, ceiling_price_inr=_num(form["CEILING_PRICE"]), shock=shock, points=points,
                        data_tags=tags)


def _explanation(request: Request, d: MoleculeDetail, form: dict[str, Any]) -> Explanation:
    """Explain the latest run's forecast once per (run, formulation, provider, model); cached in-process."""
    s: Settings = request.app.state.settings
    key = (d.run_id, d.form_id, s.llm_provider, s.llm_model)
    cache: dict = request.app.state.explain_cache
    if key not in cache:
        recs = [v.recommendation for v in d.recommendations]
        name = " ".join(x for x in (form.get("GENERIC"), form.get("STRENGTH")) if isinstance(x, str) and x)
        if len(cache) > 500:
            cache.clear()
        cache[key] = explain(d.forecast, d.dependency, orch.best_recommendation(recs, d.scenarios, d.form_id),
                             name=name or None, settings=s)
    return cache[key]


@secured.post("/run", response_model=RunResponse)
def run(req: RunRequest, request: Request, db: Db = Depends(get_conn)) -> RunResponse:
    ids = _resolve(db, req.form_ids, MAX_FORMS_PER_RUN)
    r = orch.run(ids, req.shock, db=db, rules=rules_of(request), dry_run=req.dry_run,
                 settings=request.app.state.settings)
    return RunResponse(run_id=r.run_id, status=r.status, dry_run=r.dry_run, chain_ok=r.chain.ok, signals=r.signals,
                       forecasts=r.forecasts, recommendations=r.recommendations,
                       awaiting_approval=[x.scenario_id for x in r.gate], explanations=r.explanations)


@secured.post("/scenario/shock", response_model=ShockResponse)
def scenario_shock(req: ShockRequest, request: Request, db: Db = Depends(get_conn)) -> ShockResponse:
    ids = _resolve(db, req.form_ids, 200)
    rules = rules_of(request)
    rc = RunContext(run_id=f"whatif-{uuid.uuid4().hex[:8]}", dry_run=True, as_of=orch.as_of_date(db))
    ctx = AgentCtx(db, rules, rc)
    cache: dict = {}  # secondary signals are the same for both calls: fetch them once
    base = {s.formulation_id: s for s in a1.run(ids, ctx, secondary_cache=cache)}
    shocked = a1.run(ids, ctx, api_cost_multiplier=req.multiplier, secondary_cache=cache)
    names = {r["FORM_ID"]: r for r in _records(
        db, f"SELECT FORM_ID, GENERIC, STRENGTH FROM FF_REF_FORMULATION WHERE FORM_ID IN ({placeholders(ids)})", tuple(ids))}
    rows = [ShockRow(form_id=s.formulation_id, generic=names.get(s.formulation_id, {}).get("GENERIC"),
                     strength=names.get(s.formulation_id, {}).get("STRENGTH"), baseline=base[s.formulation_id],
                     shocked=s, band_changed=base[s.formulation_id].band is not s.band) for s in shocked]
    rows.sort(key=lambda x: (not x.band_changed, x.shocked.headroom_pct))
    return ShockResponse(multiplier=req.multiplier, rows=rows)


def _approving(a: dict[str, Any] | None) -> bool:
    return bool(a) and a["ACTION"] in (ApprovalAction.APPROVE.value, ApprovalAction.EDIT_APPROVE.value)


def _awaiting_level(db: Db, rec: dict[str, Any], rules: Rules, now: datetime,
                    approvals: list[dict[str, Any]] | None = None) -> int | None:
    """1 or 2 if the recommendation still needs a human at that level, else None."""
    rec_id = rec["SCENARIO_ID"]
    latest = a6.latest_by_level(approvals if approvals is not None else a6.load_approvals(db, rec_id))
    if not latest:
        return 1
    if any(a["ACTION"] == ApprovalAction.REJECT.value for a in latest.values()):
        return None
    l1 = latest.get(1)
    if l1 and l1["ACTION"] == ApprovalAction.SNOOZE.value:
        decided = datetime.fromisoformat(a6._ts(l1["DECIDED_AT"]))
        return None if decided + timedelta(days=int(l1["SNOOZE_DAYS"] or 0)) > now else 1
    if not _approving(l1):
        return 1
    try:
        draft = a6.edited_draft(rec, latest)
    except a6.ActRefused:
        return None  # approved internal rebalance: nothing more to do
    return 2 if a6.needs_second_approver(rec, draft, rules) and not _approving(latest.get(2)) else None


@secured.get("/approvals/pending", response_model=list[PendingApproval])
def approvals_pending(request: Request, db: Db = Depends(get_conn)) -> list[PendingApproval]:
    """Gate items of each formulation's latest run that still need a human decision."""
    rules = rules_of(request)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recs = _records(db, "SELECT r.RUN_ID, r.SCENARIO_ID, r.FORM_ID, r.OVERALL, r.NEEDS_SECOND_APPROVER, r.DRAFT_PR_JSON, "
                        "r.CREATED_AT, f.GENERIC FROM FF_AG_RECOMMENDATION r "
                        "LEFT JOIN FF_REF_FORMULATION f ON f.FORM_ID = r.FORM_ID "
                        "WHERE r.OVERALL IN ('READY', 'NEEDS_CHANGES') AND NOT EXISTS (SELECT 1 FROM FF_AG_ACTION a "
                        "WHERE a.RUN_ID = r.RUN_ID AND a.SCENARIO_ID = r.SCENARIO_ID) "
                        f"ORDER BY r.CREATED_AT DESC, r.RUN_ID DESC, r.SCENARIO_ID LIMIT {PENDING_SCAN_LIMIT}")
    latest_run: dict[str, str] = {}
    current = []
    for rec in recs:
        if latest_run.setdefault(rec["FORM_ID"], rec["RUN_ID"]) == rec["RUN_ID"]:
            current.append(rec)  # older runs of the same formulation are superseded
    # One query per run for approvals, scenarios and checks instead of three per recommendation: from BTP
    # to HANA each round trip costs ~0.2 s, and ~190 recommendations took ~2 minutes.
    run_ids = sorted({r["RUN_ID"] for r in current})
    approvals: dict[str, list[dict[str, Any]]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    checks_by: dict[str, list[CheckResult]] = {}
    for run_id in run_ids:
        for a in _records(db, "SELECT SCENARIO_ID, LEVEL_NO, DECIDED_AT, APPROVER, ACTION, CHECKLIST_JSON, REASON, "
                              "EDITED_QTY, EDITED_SUPPLIER, SNOOZE_DAYS FROM FF_AG_APPROVAL WHERE RUN_ID = ? "
                              "ORDER BY DECIDED_AT", (run_id,)):
            approvals.setdefault(a.pop("SCENARIO_ID"), []).append(a)
        for s in _records(db, "SELECT * FROM FF_AG_SCENARIO WHERE RUN_ID = ?", (run_id,)):
            scenarios[s["SCENARIO_ID"]] = s
        checks_by.update(_checks(db, run_id))
    out = []
    for rec in current:
        level = _awaiting_level(db, rec, rules, now, approvals.get(rec["SCENARIO_ID"], []))
        if level is None:
            continue
        scen = scenarios.get(rec["SCENARIO_ID"])
        checks = checks_by.get(rec["SCENARIO_ID"], [])
        r = recommendation_of(rec, checks)
        out.append(PendingApproval(rec_id=rec["SCENARIO_ID"], run_id=rec["RUN_ID"], form_id=rec["FORM_ID"],
                                   generic=rec["GENERIC"], overall=rec["OVERALL"],
                                   needs_second_approver=r.needs_second_approver, awaiting_level=level,
                                   scenario=scenario_of(scen) if scen else None, draft_pr=r.draft_pr, checks=checks))
    return out


@secured.post("/approval", response_model=ApprovalResponse)
def approval(req: ApprovalRequest, request: Request, db: Db = Depends(get_conn)) -> ApprovalResponse:
    edits = req.edits.model_dump(exclude_none=True) if req.edits else None
    try:
        res = orch.decide(req.rec_id, req.user, req.decision, req.checklist, edits, req.reason, db=db,
                          rules=rules_of(request), level=req.level, snooze_days=req.snooze_days,
                          check_master=bool(request.app.state.settings.sap_api_hub_key))
    except orch.DecisionRefused as exc:
        raise HTTPException(404 if str(exc).startswith("unknown recommendation") else 422, str(exc)) from None
    except a6.ActRefused as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:  # malformed rec_id
        raise HTTPException(422, str(exc)) from None
    act = res.action
    return ApprovalResponse(rec_id=res.rec_id, status=res.status, decision=res.decision,
                            banfn=act.banfn if act else None, payload=act.payload if act else None,
                            master_data=act.master_data if act else [], notes=res.notes)


@secured.get("/audit/{run_id}", response_model=AuditResponse)
def audit(run_id: str, db: Db = Depends(get_conn)) -> AuditResponse:
    rows = a6.DbAuditLog(db).load(run_id)
    run_row = _one(db, "SELECT RUN_ID, STARTED_AT, FINISHED_AT, TRIGGER_TYPE, STATUS, FORMULATION_IDS_JSON, AS_OF_DATE, "
                       "SHOCK_JSON, DRY_RUN, RULE_VERSIONS_JSON FROM FF_AG_RUN WHERE RUN_ID = ?", (run_id,))
    if not rows and run_row is None:
        raise HTTPException(404, f"unknown run {run_id!r}")
    chain = a6.verify_rows(rows) if rows else a6.ChainCheck(False, 0, 0, "no audit rows for this run")
    return AuditResponse(run_id=run_id, run=run_row, chain=ChainStatus(**chain.__dict__),
                         events=[audit_event_of(_row(r)) for r in rows])


@secured.get("/audit/{run_id}/report", response_class=HTMLResponse)
def audit_report(run_id: str, db: Db = Depends(get_conn), rec_id: str | None = None) -> HTMLResponse:
    """NABH evidence for one decision of the run (default: the most recently decided recommendation)."""
    recs = _records(db, "SELECT r.SCENARIO_ID, MAX(a.DECIDED_AT) AS LAST_DECIDED FROM FF_AG_RECOMMENDATION r "
                        "LEFT JOIN FF_AG_APPROVAL a ON a.RUN_ID = r.RUN_ID AND a.SCENARIO_ID = r.SCENARIO_ID "
                        "WHERE r.RUN_ID = ? GROUP BY r.SCENARIO_ID", (run_id,))
    if not recs:
        raise HTTPException(404, f"no recommendations for run {run_id!r}")
    ids = [r["SCENARIO_ID"] for r in recs]
    if rec_id is None:
        decided = sorted((r for r in recs if r["LAST_DECIDED"]), key=lambda r: a6._ts(r["LAST_DECIDED"]))
        rec_id = decided[-1]["SCENARIO_ID"] if decided else sorted(ids)[0]
    elif rec_id not in ids:
        raise HTTPException(404, f"{rec_id!r} is not a recommendation of run {run_id!r}")
    return HTMLResponse(report.render(report.gather(db, rec_id)))


@secured.get("/recall/{alert_id}", response_model=RecallResponse)
def recall(alert_id: str, db: Db = Depends(get_conn)) -> RecallResponse:
    alert = _one(db, "SELECT ALERT_ID, MONTH, DRUG, BATCH, MANUFACTURER_ID, FORM_ID, REASON, SOURCE, SOURCE_URL, "
                     "FETCHED_AT, IS_PROXY FROM FF_REF_NSQ_ALERT WHERE ALERT_ID = ?", (alert_id,))
    if alert is None:
        raise HTTPException(404, f"unknown NSQ alert {alert_id!r}")
    matches = _records(db, "SELECT MATNR, FORM_ID, WERKS, PLANT_NAME, LGORT, CHARG, CLABS, CINSM, CSPEM, VFDAT, "
                           "MATCH_LEVEL FROM FF_V_RECALL_TRACE WHERE ALERT_ID = ? ORDER BY WERKS, LGORT, CHARG",
                       (alert_id,))
    return RecallResponse(alert_id=alert_id, alert=alert, matches=matches)


# ---------------------------------------------------------------- app


def create_app(settings: Settings | None = None, db_factory: Callable[[], Db] | None = None,
               rules: Rules | None = None) -> FastAPI:
    if settings is None:
        applied = cf.apply_vcap()
        if applied:
            log.info("configured from VCAP_SERVICES: %s", ", ".join(applied))
        settings = load_settings()
    app = FastAPI(title="FlowForge API", version=VERSION,
                  description="Price-controlled medicine shortage early warning. Flags are probabilistic, for review.")
    app.state.settings = settings
    app.state.db_factory = db_factory or _default_factory(settings)
    app.state.rules = rules or load_rules()
    app.state.explain_cache = {}
    extra = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=extra, allow_origin_regex=BUILD_APPS_ORIGIN_REGEX,
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["X-API-Key", "Content-Type"],
                       allow_credentials=False)
    app.include_router(public)
    app.include_router(secured)
    return app


app = create_app()
