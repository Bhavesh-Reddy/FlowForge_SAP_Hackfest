import math
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from agents import a1_margin_sentinel as a1
from agents import a2_dependency as a2
from agents import a3_forecast as a3
from agents.common import load_fixture_db
from agents.contracts import (
    CauseCode, DataQuality, DataTag, DependencyProfile, RiskSignal, RunContext, SecondarySignal, SignalBand,
)

AS_OF = date(2026, 9, 26)
REQUIRED_FACTS = {"headroom_pct", "ceiling_price_inr", "estimated_unit_cost_inr", "n_producers_min",
                  "top_origin_share", "days_of_cover"}


@pytest.fixture
def db():
    d = load_fixture_db()
    yield d
    d.close()


def _ctx(db, rules, **run):
    return SimpleNamespace(db=db, rules=rules, run=RunContext(run_id="t-a3", as_of=AS_OF, **run))


def sig(fid="X", h=0.2, trend=0.0, mtb=None, secondary=(), tag=DataTag.REAL):
    return RiskSignal(run_id="r", formulation_id=fid, band="GREEN", headroom_pct=h, headroom_trend=trend,
                      months_to_breach=mtb, realisation_inr="1.0000", unit_cost_inr=str(round(1 - h, 4)) if math.isfinite(h) else None,
                      secondary_signals=list(secondary),
                      data_quality=DataQuality(confidence=1.0, tags={"ceiling_price": tag, "bom": tag, "api_cost": tag}))


def dep(rules, fid="X", n=3, top_share=0.5, tag=DataTag.REAL):
    h = 1 / n if n else 1.0  # equal market shares
    return DependencyProfile(run_id="r", formulation_id=fid, n_producers_min=n, hhi=h, top_origin_country="CN",
                             top_origin_share=top_share, concentration=a2.concentration(n, h, top_share, rules),
                             affected_materials=["M"],
                             data_quality=DataQuality(confidence=1.0, tags={"producers": tag, "api_origin": tag}))


def hosp(usable=6000.0, daily=100.0, ved="V"):
    return a3._Hospital(["M"], ved, usable, "test", a3.Consumption(daily, "MA90", 3))


REF = {"ceiling": (2.0, DataTag.REAL), "nlem_level": "P,S,T"}


def _assess(rules, s, d, h=None):
    return a3.assess(s, d, h or hosp(), REF, AS_OF, "r", rules)


def test_exposure_hand_computed(rules):
    f = _assess(rules, sig(h=0.10, trend=-0.01, mtb=10), dep(rules, n=2, top_share=0.8))
    conc = 0.5 / 2 + 0.3 * 0.5 + 0.2 * 0.8                      # 0.56
    z = 8.0 * -0.10 + 3.0 / 10 + 2.0 * conc                     # -0.8 + 0.3 + 1.12 = 0.62
    p = 1 / (1 + math.exp(-z))
    expo = p * (1.0 * 1.2) * min(3.0, (30 + 60) / 60.0)          # V, NLEM primary; cover = 6000/100 = 60 days
    assert f.exit_risk == pytest.approx(p, abs=1e-6) and f.exposure == pytest.approx(expo, abs=1e-6)
    assert f.days_of_cover == 60 and f.stockout_date_if_exit == date(2026, 11, 25)
    assert f.exit_risk_band is SignalBand.AMBER                 # 0.65: >= 0.4, < 0.7
    assert (f.window_months_lo, f.window_months_hi) == (pytest.approx(10 * (1 - 0.5 * conc)), 16.0)


def test_known_exposure_ordering(rules):
    cases = {
        "BELOW_COST": _assess(rules, sig("BELOW_COST", h=-0.03, trend=-0.01, mtb=0.0), dep(rules, "BELOW_COST", n=1, top_share=0.9)),
        "ERODING": _assess(rules, sig("ERODING", h=0.08, trend=-0.01, mtb=8), dep(rules, "ERODING", n=3, top_share=0.6)),
        "HEALTHY": _assess(rules, sig("HEALTHY", h=0.35), dep(rules, "HEALTHY", n=6, top_share=0.3)),
        "NOT_STOCKED": a3.assess(sig("NOT_STOCKED", h=-0.05, mtb=0.0), dep(rules, "NOT_STOCKED", n=1),
                                 a3._Hospital([], None, 0.0, "none", a3.Consumption(None, "NONE", 0)), REF, AS_OF, "r", rules),
    }
    order = sorted(cases, key=lambda k: -cases[k].exposure)
    assert order == ["BELOW_COST", "ERODING", "HEALTHY", "NOT_STOCKED"]
    assert cases["BELOW_COST"].exit_risk_band is SignalBand.RED and cases["HEALTHY"].exit_risk_band is SignalBand.GREEN
    assert cases["NOT_STOCKED"].exposure == 0 and cases["NOT_STOCKED"].days_of_cover is None


def test_fewer_producers_ranks_higher_all_else_equal(rules):
    few = _assess(rules, sig("FEW"), dep(rules, "FEW", n=2))
    many = _assess(rules, sig("MANY"), dep(rules, "MANY", n=5))
    assert few.exit_risk > many.exit_risk and few.exposure > many.exposure


def test_fewer_producers_ranks_first_in_run(db, rules):
    # Give F002 its own material with the same stock and issues as F001's, so only the producer count differs.
    db.execute("INSERT INTO FF_FX_MARA SELECT 'MAT-0002', MAKTX, 'F002', MEINS, VED, COLD_CHAIN, SOURCE, SOURCE_URL, "
               "FETCHED_AT, IS_PROXY FROM FF_FX_MARA WHERE MATNR = 'MAT-0001'")
    for t in ("MCHB", "MSEG"):
        cols = [c for c in db.query(f"SELECT * FROM FF_FX_{t} LIMIT 1").columns if c != "MATNR"]
        db.execute(f"INSERT INTO FF_FX_{t} (MATNR, {', '.join(cols)}) SELECT 'MAT-0002', {', '.join(cols)} "
                   f"FROM FF_FX_{t} WHERE MATNR = 'MAT-0001'")
    db.execute("UPDATE FF_FX_FORMULATIONS SET CEILING_PRICE_INR = '1.25' WHERE FORMULATION_ID = 'F001'")
    signals = [sig("F001", h=0.12, trend=-0.005, mtb=24), sig("F002", h=0.12, trend=-0.005, mtb=24)]
    deps = [dep(rules, "F001", n=4), dep(rules, "F002", n=2)]
    out = a3.run(signals, deps, _ctx(db, rules, dry_run=True))
    assert [f.formulation_id for f in out] == ["F002", "F001"]
    assert out[0].days_of_cover == out[1].days_of_cover


@pytest.mark.parametrize("s_kw,d_kw,cause", [
    (dict(h=-0.05, mtb=0.0), dict(n=4, top_share=0.3), CauseCode.CEILING_BELOW_COST),
    (dict(h=0.10, trend=-0.05, mtb=2), dict(n=4, top_share=0.3), CauseCode.HEADROOM_ERODING),
    (dict(h=0.30), dict(n=1, top_share=0.3), CauseCode.FEW_PRODUCERS),
    (dict(h=0.30), dict(n=8, top_share=1.0), CauseCode.SINGLE_ORIGIN_API),
])
def test_cause_code(rules, s_kw, d_kw, cause):
    assert _assess(rules, sig(**s_kw), dep(rules, **d_kw)).cause_code is cause


def test_nsq_cause(rules, monkeypatch):
    s = sig(h=0.30, secondary=[SecondarySignal(kind="NSQ", value=2, tag=DataTag.REAL)])
    f = _assess(rules, s, dep(rules, n=10, top_share=0.1))
    assert f.cause_code is CauseCode.QUALITY_NSQ and f.cause_facts["nsq_alerts_recent"]["value"] == 2


def test_cause_facts_complete_and_tagged(rules):
    f = _assess(rules, sig(h=0.10, trend=-0.01, mtb=10), dep(rules, n=2))
    facts = f.cause_facts
    assert REQUIRED_FACTS <= set(facts)
    for k in REQUIRED_FACTS:
        assert facts[k]["value"] is not None and facts[k]["tag"] in {"REAL", "PROXY", "SYNTH"}, k
    assert facts["headroom_pct"]["value"] == 0.10 and facts["ceiling_price_inr"]["value"] == 2.0
    assert facts["n_producers_min"]["value"] == 2 and facts["days_of_cover"]["value"] == 60
    assert facts["exposure"]["value"] == pytest.approx(f.exposure) and facts["consumption_method"]["value"] == "MA90"
    total = sum(facts["contributions"].values())
    assert 1 / (1 + math.exp(-total)) == pytest.approx(f.exit_risk, abs=1e-5)
    # REAL inputs except the SYNTH hospital data
    assert 0 < f.confidence < 1


def test_missing_headroom_does_not_crash(rules):
    f = _assess(rules, sig(h=float("nan")), dep(rules))
    assert f.window_months_lo is None and f.cause_facts["headroom_pct"]["value"] is None


def test_ma90_on_fixtures_and_fefo_usable_stock(db, rules):
    h = a3._hospital(db, "F001", ["MAT-0001"], AS_OF, rules, use_pal=False)
    assert h.consumption.method == "MA90" and h.consumption.months_history == 3  # Jun (one issue), Jul, Aug complete
    assert h.consumption.daily == pytest.approx(9120 / 90)
    assert h.usable_qty == 8000  # B2603 expired, B2604 quarantined
    assert h.ved == "V"


def test_ets_with_long_history(rules):
    days = pd.date_range("2025-09-01", "2026-08-31", freq="D")
    issues = pd.DataFrame({"BUDAT": days.strftime("%Y-%m-%d"), "MENGE": 100.0})
    c = a3.forecast_consumption(issues, AS_OF, rules)
    assert c.method == "ETS" and c.months_history == 12
    assert c.daily == pytest.approx(100 * 30.4 / 30, rel=0.03)


def test_no_history(rules):
    c = a3.forecast_consumption(pd.DataFrame(columns=["BUDAT", "MENGE"]), AS_OF, rules)
    assert c.method == "NONE" and c.daily is None


def test_days_of_cover_view_used_when_present(db, rules):
    db.execute("CREATE TABLE FF_V_DAYS_OF_COVER (MATNR TEXT, USABLE_QTY REAL)")
    db.execute("INSERT INTO FF_V_DAYS_OF_COVER VALUES ('MAT-0001', 500)")
    h = a3._hospital(db, "F001", [], AS_OF, rules, use_pal=False)
    assert h.usable_qty == 500 and h.usable_source == "FF_V_DAYS_OF_COVER"


def test_zero_consumption_is_capped(rules):
    assert a3.days_of_cover(100, 0.0, rules) == 3650


def test_pal_not_used_on_sqlite(db):
    assert a3.pal_available(db) is False


def test_end_to_end_on_fixtures_writes_forecast(db, rules):
    ctx = _ctx(db, rules)
    out = a3.run(a1.run([], ctx), a2.run([], ctx, engine="networkx"), ctx)
    by = {f.formulation_id: f for f in out}
    assert out[0].formulation_id == "F001" and by["F001"].exposure > 0
    assert by["F002"].exposure == 0 and by["F003"].exposure == 0  # no hospital material mapped
    assert by["F001"].cause_facts["days_of_cover"]["value"] == pytest.approx(8000 / (9120 / 90), abs=1e-3)
    assert by["F001"].confidence == 0.0  # all fixtures SYNTH
    rows = db.query("SELECT FORM_ID, CAUSE_CODE, CONSUMPTION_METHOD FROM FF_AG_FORECAST WHERE RUN_ID = ?", ("t-a3",))
    assert len(rows) == 3 and set(rows["CONSUMPTION_METHOD"]) == {"MA90", "NONE"}
