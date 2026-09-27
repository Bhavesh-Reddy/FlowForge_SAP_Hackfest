import json
import math
from types import SimpleNamespace

import pytest

from agents import a1_margin_sentinel as a1
from agents.contracts import DataTag, RunContext, SignalBand
from ingest import load_hana as lh


@pytest.fixture
def db():
    d = a1._fixture_db()
    yield d
    d.close()


def _ctx(db, rules, **run):
    return SimpleNamespace(db=db, rules=rules, run=RunContext(run_id="t-run", **run))


def test_f001_headroom_hand_computed(db, rules):
    # §4.1 by hand for F001 (Amoxicillin 500 mg), as of 2026-09:
    realisation = 2.18 / 1.16 / 1.10                     # CP / (1+retailer) / (1+wholesaler)
    api_kg = 1700.0                                       # median(1680, 1700, 1720)
    unit_cost = api_kg * 0.574 / 1000 / 0.95 + 0.25 + 0.15 + 0.05
    expected = (realisation - unit_cost) / realisation    # = 0.13539...
    [s] = a1.run(["F001"], _ctx(db, rules, dry_run=True))
    assert round(s.headroom_pct, 4) == round(expected, 4) == 0.1354
    assert float(s.realisation_inr) == pytest.approx(realisation, abs=1e-4)
    assert float(s.unit_cost_inr) == pytest.approx(unit_cost, abs=1e-4)
    assert s.headroom_trend < 0
    assert s.months_to_breach == pytest.approx(s.headroom_pct / -s.headroom_trend)
    assert s.band is SignalBand.AMBER


def test_trend_uses_ols_over_window(db, rules):
    [s] = a1.run(["F001"], _ctx(db, rules, dry_run=True))
    # A01 cost = 1500 + 20*i; 3-month median with partial windows at the start: 1500, 1510, 1520, 1540, ...
    costs = [1500 + 20 * i for i in range(12)]
    medians = [sorted(costs[max(0, i - 2):i + 1])[len(costs[max(0, i - 2):i + 1]) // 2]
               if i >= 2 else sum(costs[:i + 1]) / (i + 1) for i in range(12)]
    r = 2.18 / 1.16 / 1.10
    h = [(r - (m * 0.574 / 1000 / 0.95 + 0.45)) / r for m in medians]
    n = len(h)
    xbar, ybar = (n - 1) / 2, sum(h) / n
    slope = sum((i - xbar) * (y - ybar) for i, y in enumerate(h)) / sum((i - xbar) ** 2 for i in range(n))
    assert s.headroom_trend == pytest.approx(slope, abs=1e-9)


def test_shock_moves_band(db, rules):
    base = {s.formulation_id: s for s in a1.run([], _ctx(db, rules, dry_run=True))}
    shock = {s.formulation_id: s for s in a1.run([], _ctx(db, rules, dry_run=True), api_cost_multiplier=1.3)}
    assert base["F001"].band is SignalBand.AMBER and shock["F001"].band is SignalBand.RED
    assert base["F003"].band is SignalBand.GREEN
    for f in base:
        assert shock[f].headroom_pct < base[f].headroom_pct


def test_runcontext_shock_is_used(db, rules):
    [s] = a1.run(["F001"], _ctx(db, rules, dry_run=True, shock={"api_cost_pct": 30}))
    assert s.band is SignalBand.RED


def test_missing_cost_gives_low_data_quality_not_crash(db, rules):
    for t in ("FF_REF_CEILING_PRICE", "FF_REF_FORM_API", "FF_REF_BOM_ASSUMPTION", "FF_REF_API_COST_MONTHLY"):
        db.execute(f"UPDATE {t} SET IS_PROXY = 'REAL'")
    before = {s.formulation_id: s for s in a1.run([], _ctx(db, rules, dry_run=True))}
    assert before["F001"].data_quality.confidence == 0.75  # ceiling, BOM, API cost REAL; no WPI
    db.execute("DELETE FROM FF_REF_API_COST_MONTHLY WHERE API_ID = ?", ("A01",))
    after = {s.formulation_id: s for s in a1.run([], _ctx(db, rules, dry_run=True))}
    f1 = after["F001"]
    assert f1.data_quality.confidence < before["F001"].data_quality.confidence
    assert "api_cost" not in f1.data_quality.tags
    assert math.isnan(f1.headroom_pct) and f1.months_to_breach is None and f1.unit_cost_inr is None
    assert f1.band is SignalBand.AMBER  # unknown is not safe
    assert after["F003"].data_quality.confidence == 0.75


def test_fixture_rows_are_synth_so_confidence_zero(db, rules):
    for s in a1.run([], _ctx(db, rules, dry_run=True)):
        assert s.data_quality.confidence == 0.0
        assert set(s.data_quality.tags.values()) == {DataTag.SYNTH}


def test_unknown_formulation(db, rules):
    [s] = a1.run(["NOPE"], _ctx(db, rules, dry_run=True))
    assert math.isnan(s.headroom_pct) and s.data_quality.confidence == 0.0


def test_writes_ff_ag_signal(db, rules):
    sigs = a1.run(["F001", "F003"], _ctx(db, rules))
    rows = db.query("SELECT RUN_ID, FORM_ID, BAND, HEADROOM_PCT, DATA_TAGS_JSON FROM FF_AG_SIGNAL ORDER BY FORM_ID")
    assert rows["RUN_ID"].unique().tolist() == ["t-run"]
    assert rows["FORM_ID"].tolist() == ["F001", "F003"]
    assert rows.iloc[0]["HEADROOM_PCT"] == pytest.approx(sigs[0].headroom_pct)
    assert json.loads(rows.iloc[0]["DATA_TAGS_JSON"])["api_cost"] == "SYNTH"


def test_dry_run_writes_nothing(db, rules):
    a1.run(["F001"], _ctx(db, rules, dry_run=True))
    assert int(db.query("SELECT COUNT(*) AS N FROM FF_AG_SIGNAL").iloc[0]["N"]) == 0


def put(db, table, rows):
    t = lh.schema_tables()[table]
    prov = {"SOURCE": "test", "IS_PROXY": "SYNTH"} if t.has_provenance else {}
    lh.upsert_rows(db, t, [{**r, **prov} for r in rows])


def test_secondary_signals(db, rules):
    put(db, "FF_REF_NSQ_ALERT", [
        {"ALERT_ID": "N1", "MONTH": "2026-05-01", "DRUG": "Amoxicillin Capsules IP 500 mg", "BATCH": "B1",
         "MANUFACTURER_ID": "M01", "FORM_ID": "F001"},
        {"ALERT_ID": "N2", "MONTH": "2024-01-01", "DRUG": "Amoxicillin Capsules IP 500 mg", "BATCH": "B0",
         "MANUFACTURER_ID": "M02", "FORM_ID": "F001"},                                   # outside 12 months
        {"ALERT_ID": "N3", "MONTH": "2026-06-01", "DRUG": "Paracetamol Tablets", "BATCH": "B2",
         "MANUFACTURER_ID": "M01", "FORM_ID": "F003"},                                   # other formulation
        {"ALERT_ID": "N4", "MONTH": "2026-07-01", "DRUG": "Amoxicillin Capsules", "BATCH": "B3",
         "MANUFACTURER_ID": "M03", "FORM_ID": None},                                     # matched via producer
    ])
    # OTD: 6 monthly PO lines; first 3 on time, last 3 late by 10 days -> drop 100 pts
    for k, month in enumerate(("04", "05", "06", "07", "08", "09")):
        eindt = f"2026-{month}-05"
        gr = eindt if k < 3 else f"2026-{month}-15"
        put(db, "FF_MM_EKKO", [{"EBELN": f"PO{k}", "LIFNR": "V1", "BEDAT": f"2026-{month}-01"}])
        put(db, "FF_MM_EKPO", [{"EBELN": f"PO{k}", "EBELP": 10, "MATNR": "MAT-0001", "WERKS": "H001", "MENGE": 100,
                                "EINDT": eindt}])
        put(db, "FF_MM_MSEG", [{"MBLNR": f"50{k}", "MJAHR": 2026, "ZEILE": 1, "BWART": "101", "MATNR": "MAT-0001",
                                "WERKS": "H001", "MENGE": 100, "BUDAT": gr, "EBELN": f"PO{k}", "EBELP": 10}])
    put(db, "FF_MM_COLDCHAIN", [
        {"LOCATION": "CENT", "TS": "2026-09-10T03:00:00", "TEMP_C": 11.2, "EXCURSION_FLAG": 1},
        {"LOCATION": "CENT", "TS": "2026-01-10T03:00:00", "TEMP_C": 10.0, "EXCURSION_FLAG": 1},
        {"LOCATION": "CENT", "TS": "2026-09-11T03:00:00", "TEMP_C": 5.0, "EXCURSION_FLAG": 0},
        {"LOCATION": "OTHER", "TS": "2026-09-12T03:00:00", "TEMP_C": 12.0, "EXCURSION_FLAG": 1}])
    [f1] = a1.run(["F001"], _ctx(db, rules, dry_run=True))
    by = {x.kind: x for x in f1.secondary_signals}
    assert by["NSQ"].value == 2 and "CDSCO" in by["NSQ"].note
    assert by["OTD"].value == pytest.approx(100.0) and "FF_V_SUPPLIER_OTD" in by["OTD"].note
    assert by["COLD_CHAIN"].value == 1
    [f2] = a1.run(["F002"], _ctx(db, rules, dry_run=True))
    assert f2.secondary_signals == []  # N4 (M03, no FORM_ID) matches F001 via producer M03; M03 does not make F002
    [f3] = a1.run(["F003"], _ctx(db, rules, dry_run=True))
    assert [x.kind for x in f3.secondary_signals] == ["NSQ"] and f3.secondary_signals[0].value == 1


def test_missing_optional_tables_are_skipped(db, rules):
    [s] = a1.run(["F002"], _ctx(db, rules, dry_run=True))
    assert s.secondary_signals == []


@pytest.mark.parametrize("raw,tag", [("REAL", DataTag.REAL), ("proxy", DataTag.PROXY), ("SYNTH", DataTag.SYNTH),
                                     ("synthetic", DataTag.SYNTH), (None, DataTag.SYNTH), ("N", DataTag.REAL)])
def test_to_tag(raw, tag):
    assert a1.to_tag(raw) is tag


def test_band_rules(rules):
    assert a1.band_for(0.04, None, rules) is SignalBand.RED
    assert a1.band_for(0.30, 5, rules) is SignalBand.RED
    assert a1.band_for(0.14, None, rules) is SignalBand.AMBER
    assert a1.band_for(0.30, 11, rules) is SignalBand.AMBER
    assert a1.band_for(0.30, None, rules) is SignalBand.GREEN
    assert a1.months_to_breach(0.1, 0.01) is None
    assert a1.months_to_breach(-0.1, -0.01) == 0.0


def test_cli(capsys):
    assert a1.main(["--form", "F001", "--shock", "1.3", "--fixtures", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "F001" in out and "RED" in out
