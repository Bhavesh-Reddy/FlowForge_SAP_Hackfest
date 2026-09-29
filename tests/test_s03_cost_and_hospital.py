"""S03: TradeStat unit-value maths, SYNTH cost build, and the synthetic hospital generator."""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from ingest import synth_hospital as sh
from ingest import tradestat as ts

SEED = Path(__file__).resolve().parents[1] / "data" / "seed"


def _read(name: str) -> list[dict[str, str]]:
    with (SEED / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- unit value


def test_unit_value_converts_crore_and_tonnes_to_inr_per_kg():
    # ₹ 1.5 crore for 3 tonnes -> 1.5e7 / 3000 kg
    assert ts.unit_value_inr_per_kg(1.5, "crore", 3, "TON") == pytest.approx(5000.0)
    assert ts.unit_value_inr_per_kg(12.0, "lakh", 400, "KGS") == pytest.approx(3000.0)
    assert ts.unit_value_inr_per_kg(250000, "INR", 500, "gms") == pytest.approx(500000.0)


@pytest.mark.parametrize("value, qty, unit", [(0.0, 10, "KGS"), (1.0, 0, "KGS"), (1.0, 10, "LTR")])
def test_unit_value_is_none_when_it_cannot_be_computed(value, qty, unit):
    assert ts.unit_value_inr_per_kg(value, "crore", qty, unit) is None


def test_rolling_median_is_trailing_and_skips_gaps():
    assert ts.rolling_median([100, 300, 200, None, 1000]) == [100, 200, 200, 250, 600]


def test_api_budget_gives_the_requested_headroom():
    # ceiling 2.00 -> realisation 2/1.16/1.10; 500 mg API, 95% yield, 0.10 other cost, 20% headroom
    b = ts.api_budget_per_kg(2.0, 0.10, 500, 0.95, 0.20, 0.16, 0.10)
    real = 2.0 / 1.16 / 1.10
    unit_cost = b * 500 / 1e6 / 0.95 + 0.10
    assert (real - unit_cost) / real == pytest.approx(0.20)
    assert ts.api_budget_per_kg(0.10, 0.50, 500, 0.95, 0.2, 0.16, 0.10) is None  # other cost alone too high


def test_synth_cost_is_deterministic_tagged_and_complete():
    first, second = ts.build(*ts.load_inputs()), ts.build(*ts.load_inputs())
    assert first == second
    cost = first["FF_REF_API_COST_MONTHLY"]
    assert {r["IS_PROXY"] for r in cost} == {"SYNTH"}
    assert {r["IS_PROXY"] for r in first["FF_REF_FORM_API"]} == {"PROXY"}
    months = {r["MONTH"] for r in cost}
    assert min(months) == "2019-01-01" and max(months) == "2026-09-01"
    shares: dict[tuple[str, str], float] = {}
    for r in first["FF_REF_API_ORIGIN"]:
        shares[(r["API_ID"], r["MONTH"])] = shares.get((r["API_ID"], r["MONTH"]), 0) + r["SHARE"]
    assert all(abs(s - 1) < 1e-4 for s in shares.values())


# ---------------------------------------------------------------- hospital generator


@pytest.fixture(scope="module")
def inputs():
    targets = _read("targets.csv")
    ceiling = {r["FORM_ID"]: float(r["CEILING_PRICE"])
               for r in sorted(_read("FF_REF_CEILING_PRICE.csv"), key=lambda r: r["EFFECTIVE_FROM"])}
    nsq = [{"ALERT_ID": "T1", "MONTH": "2025-01-01", "FORM_ID": "METRONIDAZOLE-013A41E8",
            "BATCH": "TEST-NSQ-01", "MANUFACTURER_ID": "MFR-T"},
           {"ALERT_ID": "T2", "MONTH": "2025-02-01", "FORM_ID": "", "BATCH": "OTHER-9", "MANUFACTURER_ID": "MFR-U"}]
    return targets, {}, ceiling, nsq


@pytest.fixture(scope="module")
def generated(inputs):
    return sh.generate(*inputs)


def test_generator_is_deterministic(inputs, generated):
    assert sh.generate(*inputs) == generated


def test_shapes_locations_and_movements(generated):
    assert len(generated["FF_MM_MARA"]) == 40
    assert {r["LGORT"] for r in generated["FF_MM_MCHB"]} <= set(sh.LOCATIONS)
    assert {r["BWART"] for r in generated["FF_MM_MSEG"]} >= {"101", "261", "311", "122", "551"}
    assert all(r["IS_PROXY"] == "SYNTH" for t in generated.values() for r in t if "IS_PROXY" in r)
    days = {r["BUDAT"] for r in generated["FF_MM_MSEG"]}
    assert min(days) >= (sh.AS_OF - timedelta(days=560)).isoformat() and max(days) <= sh.AS_OF.isoformat()
    assert {r["VED"] for r in generated["FF_MM_MARA"]} <= {"V", "E", "D"}
    assert {r["ABC"] for r in generated["FF_MM_MARA"]} == {"A", "B", "C"}


def test_exactly_one_planted_nsq_batch_at_several_locations(generated):
    planted = [r for r in generated["FF_MM_MCHB"] if r["CHARG"] == "TEST-NSQ-01"]
    assert len({r["LGORT"] for r in planted}) >= 3  # recall demo: 3 locations
    assert {r["MANUFACTURER_ID"] for r in planted} == {"MFR-T"}
    assert all(float(r["CLABS"]) > 0 for r in planted)
    assert not [r for r in generated["FF_MM_MCHB"] if r["CHARG"] == "OTHER-9"]
    assert all(r["CHARG"].startswith("SY") for r in generated["FF_MM_MCHB"] if r["CHARG"] != "TEST-NSQ-01")


def test_net_prices_within_ceiling_and_some_late_deliveries(generated, inputs):
    _, _, ceiling, _ = inputs
    form = {r["MATNR"]: r["FORM_ID"] for r in generated["FF_MM_MARA"]}
    assert all(float(p["NETPR"]) <= ceiling[form[p["MATNR"]]] for p in generated["FF_MM_EKPO"])
    gr = {(r["EBELN"], str(r["EBELP"])): r["BUDAT"] for r in generated["FF_MM_MSEG"] if r["BWART"] == "101"}
    late = [p for p in generated["FF_MM_EKPO"] if (p["EBELN"], str(p["EBELP"])) in gr and gr[(p["EBELN"], str(p["EBELP"]))] > p["EINDT"]]
    assert 0 < len(late) < len(generated["FF_MM_EKPO"])


def test_exactly_two_cold_chain_excursions(generated):
    assert sum(r["EXCURSION_FLAG"] for r in generated["FF_MM_COLDCHAIN"]) == 2


def test_planting_fails_loudly_without_a_matching_alert(inputs):
    targets, prod, ceiling, _ = inputs
    with pytest.raises(ValueError, match="no CDSCO NSQ alert"):
        sh.generate(targets, prod, ceiling, [{"ALERT_ID": "X", "MONTH": "2025-01-01", "FORM_ID": "", "BATCH": "B",
                                              "MANUFACTURER_ID": "M"}])
