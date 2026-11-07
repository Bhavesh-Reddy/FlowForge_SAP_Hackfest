"""Reproduce the F001 S1-S5 sensitivity runs on the SYNTH test fixtures.

Run from the repository root with ``python scripts/f001_sensitivity.py``.
S4 changes only unrestricted live batches: B2601 CLABS 6000 -> 1593.75 and
B2602 CLABS 2000 -> 531.25. Expired B2603 and quarantined B2604 are unchanged.
S5 combines the S2 API-cost, S3 producer-share and S4 stock changes.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import a1_margin_sentinel as a1  # noqa: E402
from agents import a2_dependency as a2  # noqa: E402
from agents import a3_forecast as a3  # noqa: E402
from agents.common import load_fixture_db  # noqa: E402
from agents.contracts import RunContext  # noqa: E402
from agents.rules import Rules, load_rules  # noqa: E402

AS_OF = date(2026, 9, 26)
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "results" / "f001_sensitivity.csv"
S4_BATCHES = (("B2601", 6000.0, 1593.75), ("B2602", 2000.0, 531.25))
COLUMNS = (
    "scenario", "headroom", "concentration", "a3_risk", "a1_band", "a3_band", "days_of_cover",
    "daily_consumption", "usable_stock", "cover_ratio_before_clamp", "exposure",
)


def run_scenario(
    scenario: str,
    rules: Rules,
    *,
    api_cost_multiplier: float = 1.0,
    producer_shares: tuple[float, float, float] | None = None,
    reduce_stock: bool = False,
) -> dict[str, Any]:
    """Run A1-A3 for F001 in an isolated copy of the fixture database."""
    db = load_fixture_db(as_of=AS_OF.isoformat())
    try:
        if producer_shares is not None:
            for manufacturer_id, share in zip(("M01", "M02", "M03"), producer_shares):
                changed = db.execute(
                    "UPDATE FF_REF_PRODUCER SET MARKET_SHARE = ? WHERE FORM_ID = ? AND MANUFACTURER_ID = ?",
                    (share, "F001", manufacturer_id),
                )
                if changed != 1:
                    raise RuntimeError(f"Expected one F001 producer row for {manufacturer_id}; updated {changed}")

        if reduce_stock:
            for batch, original_qty, new_qty in S4_BATCHES:
                current = db.query(
                    "SELECT CLABS FROM FF_MM_MCHB WHERE MATNR = ? AND CHARG = ?", ("MAT-0001", batch)
                )
                if len(current) != 1 or float(current.iloc[0]["CLABS"]) != original_qty:
                    raise RuntimeError(f"Unexpected starting CLABS for MAT-0001 batch {batch}")
                changed = db.execute(
                    "UPDATE FF_MM_MCHB SET CLABS = ? WHERE MATNR = ? AND CHARG = ?",
                    (new_qty, "MAT-0001", batch),
                )
                if changed != 1:
                    raise RuntimeError(f"Expected one MAT-0001 batch {batch}; updated {changed}")

        ctx = SimpleNamespace(
            db=db,
            rules=rules,
            run=RunContext(run_id=f"f001-{scenario.lower()}", as_of=AS_OF, dry_run=True),
        )
        [signal] = a1.run(["F001"], ctx, api_cost_multiplier=api_cost_multiplier)
        [dependency] = a2.run(["F001"], ctx, engine="networkx")
        [forecast] = a3.run([signal], [dependency], ctx)

        facts = forecast.cause_facts
        usable_stock = float(facts["usable_stock_qty"]["value"])
        daily_consumption = float(facts["daily_consumption"]["value"])
        raw_cover = a3.days_of_cover(usable_stock, daily_consumption, rules)
        if raw_cover is None:
            cover_ratio = None
        else:
            need = float(rules.value("weights", "exposure.lead_time_days")) + float(
                rules.value("weights", "exposure.requal_days")
            )
            floor = float(rules.value("weights", "exposure.days_of_cover_floor"))
            cover_ratio = need / max(raw_cover, floor)

        return {
            "scenario": scenario,
            "headroom": round(signal.headroom_pct, 6),
            "concentration": dependency.concentration,
            "a3_risk": forecast.exit_risk,
            "a1_band": signal.band.value,
            "a3_band": forecast.exit_risk_band.value,
            "days_of_cover": forecast.days_of_cover,
            "daily_consumption": daily_consumption,
            "usable_stock": usable_stock,
            "cover_ratio_before_clamp": None if cover_ratio is None else round(cover_ratio, 6),
            "exposure": forecast.exposure,
        }
    finally:
        db.close()


def run_scenarios(rules: Rules) -> list[dict[str, Any]]:
    return [
        run_scenario("S1", rules),
        run_scenario("S2", rules, api_cost_multiplier=1.3),
        run_scenario("S3", rules, producer_shares=(0.98, 0.01, 0.01)),
        run_scenario("S4", rules, reduce_stock=True),
        run_scenario(
            "S5",
            rules,
            api_cost_multiplier=1.3,
            producer_shares=(0.98, 0.01, 0.01),
            reduce_stock=True,
        ),
    ]


def main() -> int:
    results = run_scenarios(load_rules())
    table = pd.DataFrame(results, columns=COLUMNS)
    print("S4/S5 batch changes: B2601 CLABS 6000 -> 1593.75; B2602 CLABS 2000 -> 531.25.")
    print("B2603 (expired) and B2604 (quarantined) are unchanged.")
    print("S5 combines S2 API cost x1.3, S3 producer shares 98/1/1, and S4 stock 2,125.")
    print(table.to_string(index=False))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUTPUT_PATH, index=False)
    print(f"\nSaved {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())