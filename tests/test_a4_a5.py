import copy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import a4_resilience as a4
from agents import a5_validator as a5
from agents.contracts import (
    CauseCode, CheckStatus, Forecast, OptionType, Overall, RunContext, Scenario, SignalBand,
)
from agents.ctx import Settings, get_db
from ingest import load_hana as lh

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 9, 26)
DAILY = 9120 / 90  # fixture MA90 consumption of MAT-0001
USABLE = 8000.0


def put(db, table, rows):
    t = lh.schema_tables()[table]
    prov = {"SOURCE": "test", "IS_PROXY": "SYNTH"} if t.has_provenance else {}
    lh.upsert_rows(db, t, [{**r, **prov} for r in rows])


def po(db, ebeln, vendor, material, netpr, bedat="2026-08-01"):
    put(db, "FF_MM_EKKO", [{"EBELN": ebeln, "LIFNR": vendor, "BEDAT": bedat}])
    put(db, "FF_MM_EKPO", [{"EBELN": ebeln, "EBELP": 10, "MATNR": material, "WERKS": "H001", "MENGE": 1000,
                            "NETPR": netpr, "PEINH": 1, "EINDT": bedat}])


@pytest.fixture
def db(tmp_path):
    d = get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "s07.sqlite")
    lh.create_objects(d)
    lh.seed(d, [FIXTURES])
    put(d, "FF_CFG_PARAM", [{"NAME": "AS_OF_DATE", "DATE_VALUE": AS_OF.isoformat()}])
    put(d, "FF_MM_LFA1", [{"LIFNR": m, "NAME1": f"Fixture Pharma {m}", "GSTIN": f"33AAAAA{m}Z5", "DRUG_LICENCE_NO": f"TN/DL/{m}"}
                          for m in ("M01", "M02", "M03", "M04")])
    po(d, "PO1", "M01", "MAT-0001", 1.90)
    d.execute("UPDATE FF_REF_FORMULATION SET THERAPEUTIC_CLASS = 'PENICILLINS' WHERE FORM_ID IN ('F001', 'F002', 'F003')")
    put(d, "FF_MM_MARA", [{"MATNR": "MAT-0002", "FORM_ID": "F002", "MEINS": "EA"}])  # F002 is on the formulary
    yield d
    d.close()


def _ctx(db, rules, **run):
    return SimpleNamespace(db=db, rules=rules, run=RunContext(run_id="t-s07", as_of=AS_OF, **run))


def forecast(fid="F001", hi=12.0, materials=("MAT-0001",), daily=DAILY, usable=USABLE):
    return Forecast(run_id="t-s07", formulation_id=fid, exit_risk_band=SignalBand.AMBER, exit_risk=0.6, exposure=0.8,
                    window_months_lo=min(6.0, hi), window_months_hi=hi, days_of_cover=usable / daily,
                    cause_code=CauseCode.HEADROOM_ERODING,
                    cause_facts={"hospital_materials": {"value": list(materials)},
                                 "daily_consumption": {"value": daily}, "usable_stock_qty": {"value": usable}})


def with_rule(rules, file, path, value):
    r = copy.deepcopy(rules)
    node = r.docs[file]
    parts = path.split(".")
    for p in parts[:-1]:
        node = node[p]
    node[parts[-1]]["value"] = value
    return r


def with_param(rules, check_id, name, value):
    r = copy.deepcopy(rules)
    next(c for c in r.docs["sop_controls"]["checks"] if c["id"] == check_id)["params"][name]["value"] = value
    return r


# ---------------------------------------------------------------- A4


def test_buffer_sized_to_cover_window(db, rules):
    out = a4.run([forecast()], _ctx(db, rules, dry_run=True))
    top = out[0]
    assert top.rank == 1 and top.option_type is OptionType.BUFFER and top.supplier == "M01"
    horizon = min(365, 12 * a4.DAYS_PER_MONTH)  # capped by resilience.max_horizon_days
    shortfall = int(-(-(DAILY * horizon - USABLE) // 1))
    assert top.qty == shortfall and top.coverage_days >= horizon - 0.01
    assert float(top.unit_rate_inr) == pytest.approx(1.90 * 1.05, abs=1e-4)  # PO net + GST
    assert float(top.cost) == pytest.approx(float(top.unit_rate_inr) * top.qty, abs=0.01)


def test_correlated_alternate_rejected_next_best_chosen(db, rules):
    po(db, "PO2", "M02", "MAT-0001", 1.50, bedat="2026-07-01")  # M02 is cheaper than the current supplier ...
    out = a4.run([forecast()], _ctx(db, rules, dry_run=True))
    alt = next(s for s in out if s.supplier == "M02")
    assert alt.correlated_risk_flag and alt.rank is None           # ... but shares CN origin -> rejected
    assert "origin CN" in alt.correlated_risk_reason
    ranked = [s for s in out if s.rank is not None]
    assert ranked[0].option_type is OptionType.BUFFER and ranked[0].supplier == "M01"
    assert all(not s.correlated_risk_flag for s in ranked)


def test_correlated_alternate_penalised_when_configured(db, rules):
    po(db, "PO2", "M02", "MAT-0001", 1.50, bedat="2026-07-01")
    r = with_rule(rules, "weights", "resilience.correlated_risk_action", "penalise")
    out = a4.run([forecast()], _ctx(db, r, dry_run=True))
    alt = next(s for s in out if s.supplier == "M02")
    assert alt.correlated_risk_flag and alt.rank is not None


def test_uncorrelated_alternate_can_win(db, rules):
    # Different API origin for the therapeutic alternative's API and no shared KSM -> not correlated.
    db.execute("UPDATE FF_REF_FORM_API SET API_ID = 'A02' WHERE FORM_ID = 'F002'")
    db.execute("UPDATE FF_REF_API_ORIGIN SET COUNTRY = 'IN' WHERE API_ID = 'A02'")
    out = a4.run([forecast()], _ctx(db, rules, dry_run=True))
    ther = next(s for s in out if s.option_type is OptionType.THERAPEUTIC_ALT)
    assert not ther.correlated_risk_flag and ther.rank is not None and ther.substitute_formulation_id == "F002"


def test_therapeutic_alternates_come_from_the_hospital_formulary(db, rules):
    # F003 shares the class but has no hospital material (FF_MM_MARA), so it is never offered
    out = a4.run([forecast()], _ctx(db, rules, dry_run=True))
    subs = {s.substitute_formulation_id for s in out if s.option_type is OptionType.THERAPEUTIC_ALT}
    assert subs == {"F002"}


def test_buffer_never_exceeds_expiry_safe_qty(db, rules):
    r = with_rule(rules, "weights", "resilience.offered_shelf_life_months", 6)
    s = a4.load_situation(db, forecast(hi=24.0), AS_OF, r)
    safe = a4.expiry_safe_qty(s, r)
    assert 0 < safe < s.shortfall  # the window needs more than can be used before expiry
    for sc in a4.run([forecast(hi=24.0)], _ctx(db, r, dry_run=True)):
        if sc.option_type is not OptionType.REBALANCE:
            assert sc.qty <= safe and sc.expiry_waste_risk == 0.0


def test_moq_above_expiry_safe_drops_option(db, rules):
    r = with_rule(with_rule(rules, "weights", "resilience.offered_shelf_life_months", 3),
                  "weights", "resilience.default_moq_units", 10**6)
    assert a4.run([forecast()], _ctx(db, r, dry_run=True)) == []


def test_budget_limits_qty(db, rules):
    r = with_param(rules, "BUDGET", "budget_remaining_inr", 1000)
    out = [s for s in a4.run([forecast()], _ctx(db, r, dry_run=True)) if s.option_type is OptionType.BUFFER]
    assert float(out[0].cost) <= 1000


def test_not_stocked_or_covered_gives_no_scenarios(db, rules):
    assert a4.run([forecast(materials=())], _ctx(db, rules, dry_run=True)) == []
    assert a4.run([forecast(hi=0.5)], _ctx(db, rules, dry_run=True)) == []  # 15 days < 79 days of cover


def test_rebalance_between_locations(db, rules):
    put(db, "FF_MM_MSEG", [{"MBLNR": f"49R{k}", "MJAHR": 2026, "ZEILE": 1, "BWART": "201", "MATNR": "MAT-0001",
                            "WERKS": "H001", "LGORT": "ONCO", "MENGE": 700, "BUDAT": f"2026-09-{10 + k:02d}"}
                           for k in range(4)])
    out = a4.run([forecast()], _ctx(db, rules, dry_run=True))
    rb = next(s for s in out if s.option_type is OptionType.REBALANCE)
    assert rb.qty > 0 and rb.unit_rate_inr is None and rb.rank is not None


def test_writes_ff_ag_scenario(db, rules):
    out = a4.run([forecast()], _ctx(db, rules))
    rows = db.query("SELECT SCENARIO_ID, OPTION_TYPE, RANK_NO, CORRELATED_RISK_FLAG FROM FF_AG_SCENARIO")
    assert len(rows) == len(out) and set(rows["OPTION_TYPE"]) >= {"BUFFER"}


# ---------------------------------------------------------------- A5


def _scenario(**kw):
    base = dict(scenario_id="t-s07:F001:01", run_id="t-s07", formulation_id="F001", option_type=OptionType.BUFFER,
                supplier="M01", material="MAT-0001", qty=5000, unit_rate_inr="2.0000", cost="10000",
                coverage_days=120, plant="H001", residual_shelf_life_months=24, rank=1)
    return Scenario(**{**base, **kw})


def _check(rec, cid):
    return next(c for c in rec.checks if c.check_id == cid)


def test_all_ten_checks_ready(db, rules):
    rec = a5.validate(_scenario(), forecast(), db, rules, AS_OF)
    assert len(rec.checks) == 10 and rec.overall is Overall.READY and not rec.needs_second_approver
    assert all(c.rule_source and "rule" in c.evidence for c in rec.checks)
    pr = rec.draft_pr.to_s4_payload()
    assert pr["Material"] == "MAT-0001" and pr["FixedSupplier"] == "M01" and pr["RequestedQuantity"] == 5000
    assert pr["DeliveryDate"] == "2026-10-26" and pr["PurchaseRequisitionPrice"] == "2.0000"


def test_price_above_ceiling_plus_gst_is_blocked(db, rules):
    # F001 ceiling 2.18 excl. GST -> max 2.289 incl. 5% GST
    ok = a5.validate(_scenario(unit_rate_inr="2.2890"), forecast(), db, rules, AS_OF)
    assert _check(ok, "PRICE_COMPLIANCE").status is CheckStatus.PASS
    bad = a5.validate(_scenario(unit_rate_inr="2.2900"), forecast(), db, rules, AS_OF)
    price = _check(bad, "PRICE_COMPLIANCE")
    assert price.status is CheckStatus.FAIL and price.evidence["max_allowed_inr"] == pytest.approx(2.289)
    assert "para 14" in price.rule_source and bad.overall is Overall.BLOCKED and not bad.reaches_gate


def test_price_blocked_end_to_end_from_po_rate(db, rules):
    po(db, "PO9", "M01", "MAT-0001", 2.30, bedat="2026-09-01")  # net 2.30 -> 2.415 incl. GST > 2.289
    ctx = _ctx(db, rules, dry_run=True)
    recs = a5.run(a4.run([forecast()], ctx), [forecast()], ctx)
    buf = next(r for r in recs if r.scenario_id.endswith(":01"))
    assert buf.overall is Overall.BLOCKED


def test_therapeutic_alt_always_needs_second_approver(db, rules):
    s = _scenario(option_type=OptionType.THERAPEUTIC_ALT, substitute_formulation_id="F002", material=None,
                  unit_rate_inr="1.0000", cost="100", qty=100)
    rec = a5.validate(s, forecast(), db, rules, AS_OF)
    assert rec.needs_second_approver and _check(rec, "CLINICAL").status is CheckStatus.WARN
    assert rec.overall is Overall.NEEDS_CHANGES and rec.draft_pr.material == "NEW-F002"


def test_value_above_threshold_needs_second_approver(db, rules):
    rec = a5.validate(_scenario(cost="150000"), forecast(), db, rules, AS_OF)
    assert rec.needs_second_approver and _check(rec, "BUDGET").status is CheckStatus.PASS


def test_supplier_checks(db, rules):
    db.execute("UPDATE FF_MM_LFA1 SET SPERR = 'X' WHERE LIFNR = 'M01'")
    assert _check(a5.validate(_scenario(), forecast(), db, rules, AS_OF), "SUPPLIER_LICENCE").status is CheckStatus.FAIL
    unknown = a5.validate(_scenario(supplier="M99"), forecast(), db, rules, AS_OF)
    assert _check(unknown, "SUPPLIER_LICENCE").status is CheckStatus.WARN


def test_quality_history(db, rules):
    put(db, "FF_REF_NSQ_ALERT", [{"ALERT_ID": "N1", "MONTH": "2024-01-01", "DRUG": "Amoxicillin", "BATCH": "B",
                                  "MANUFACTURER_ID": "M01", "FORM_ID": "F001"}])
    assert _check(a5.validate(_scenario(), forecast(), db, rules, AS_OF), "QUALITY_HISTORY").status is CheckStatus.WARN
    put(db, "FF_REF_NSQ_ALERT", [{"ALERT_ID": "N2", "MONTH": "2026-06-01", "DRUG": "Amoxicillin", "BATCH": "B2",
                                  "MANUFACTURER_ID": "M01", "FORM_ID": "F001"}])
    rec = a5.validate(_scenario(), forecast(), db, rules, AS_OF)
    assert _check(rec, "QUALITY_HISTORY").status is CheckStatus.FAIL and rec.overall is Overall.BLOCKED


def test_shelf_life_expiry_storage_rol(db, rules):
    rec = a5.validate(_scenario(residual_shelf_life_months=6, expiry_waste_risk=0.2, qty=10**6, cold_chain=True),
                      forecast(), db, rules, AS_OF)
    for cid in ("SHELF_LIFE", "EXPIRY_WASTE", "STORAGE"):
        assert _check(rec, cid).status is CheckStatus.FAIL, cid
    assert _check(rec, "ROL_ROQ").status is CheckStatus.WARN  # on_fail: WARN in sop_controls.yaml


def test_rebalance_has_no_pr(db, rules):
    s = _scenario(option_type=OptionType.REBALANCE, supplier=None, unit_rate_inr=None, cost="10", qty=200)
    rec = a5.validate(s, forecast(), db, rules, AS_OF)
    assert rec.draft_pr is None and rec.overall is Overall.READY


def test_run_writes_recommendation_and_checks(db, rules):
    ctx = _ctx(db, rules)
    scen = a4.run([forecast()], ctx)
    recs = a5.run(scen, [forecast()], ctx)
    assert len(recs) == len([s for s in scen if s.rank is not None])
    n_rec = db.query("SELECT COUNT(*) AS N FROM FF_AG_RECOMMENDATION").iloc[0]["N"]
    n_chk = db.query("SELECT COUNT(*) AS N FROM FF_AG_CHECK").iloc[0]["N"]
    assert n_rec == len(recs) and n_chk == 10 * len(recs)
