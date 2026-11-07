"""Run A1-A3 for the 35 BOM-supported targets and a pre-revision price check.

Run from the repository root with ``python scripts/batch_all_targets.py``.
"""
from __future__ import annotations

import csv
import json
import math
import sys
import uuid
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import a1_margin_sentinel as a1  # noqa: E402
from agents import a2_dependency as a2  # noqa: E402
from agents import a3_forecast as a3  # noqa: E402
from agents.contracts import RunContext  # noqa: E402
from agents.ctx import get_db  # noqa: E402
from agents.rules import load_rules  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS_PATH = REPO_ROOT / "data" / "seed" / "targets.csv"
BOM_PATH = REPO_ROOT / "data" / "seed" / "FF_REF_BOM_ASSUMPTION.csv"
OUTPUT_PATH = REPO_ROOT / "results" / "batch_35.csv"
AS_OF = date(2026, 9, 26)
HISTORICAL_AS_OF = date(2026, 6, 10)
HISTORICAL_TARGETS = {
    "CARBOPLATIN-49C2E049": 60.49,
    "CISPLATIN-1C3AF2A6": 7.26,
}
FIXTURE_IDS = {"F001", "F002", "F003"}
BANDS = ("RED", "AMBER", "GREEN")
CSV_COLUMNS = (
    "FORM_ID",
    "generic name",
    "headroom",
    "A1 band",
    "n_producers_min",
    "hhi",
    "concentration",
    "A3 risk score",
    "A3 band",
    "input provenance tags",
)


def load_targets() -> list[dict[str, str]]:
    with TARGETS_PATH.open(encoding="utf-8-sig", newline="") as stream:
        targets = {row["FORM_ID"]: row for row in csv.DictReader(stream)}
    with BOM_PATH.open(encoding="utf-8-sig", newline="") as stream:
        bom_ids = {row["FORM_ID"] for row in csv.DictReader(stream)}

    ids = sorted((set(targets) & bom_ids) - FIXTURE_IDS)
    if len(ids) != 35:
        raise RuntimeError(
            f"Expected 35 BOM-supported non-fixture targets; found {len(ids)}"
        )
    return [{"FORM_ID": fid, "GENERIC": targets[fid]["GENERIC"]} for fid in ids]


def inspect_inputs(db: Any, form_id: str, as_of: date) -> dict[str, Any]:
    """Return only observed input availability; never supply a default value."""
    formulation = db.query(
        "SELECT NLEM_LEVEL, IS_PROXY FROM FF_REF_FORMULATION WHERE FORM_ID = ?",
        (form_id,),
    )
    bom = db.query(
        "SELECT YIELD, CONVERSION_COST_INR, PACKAGING_COST_INR, FREIGHT_COST_INR, IS_PROXY "
        "FROM FF_REF_BOM_ASSUMPTION WHERE FORM_ID = ?",
        (form_id,),
    )
    form_api = db.query(
        "SELECT API_ID, API_MG_PER_UNIT, IS_PROXY FROM FF_REF_FORM_API WHERE FORM_ID = ?",
        (form_id,),
    )
    ceiling = db.query(
        "SELECT EFFECTIVE_FROM, CEILING_PRICE, IS_PROXY FROM FF_REF_CEILING_PRICE "
        "WHERE FORM_ID = ? AND EFFECTIVE_FROM <= ? ORDER BY EFFECTIVE_FROM",
        (form_id, as_of.isoformat()),
    )
    api_cost = None
    if len(form_api):
        api_id = str(form_api.iloc[0]["API_ID"])
        api_cost = db.query(
            "SELECT MONTH, UNIT_VALUE_INR_KG, IS_PROXY FROM FF_REF_API_COST_MONTHLY "
            "WHERE API_ID = ? AND MONTH <= ? ORDER BY MONTH",
            (api_id, as_of.isoformat()),
        )
    else:
        api_id = None
    producers = db.query(
        "SELECT MANUFACTURER_ID, IS_PROXY FROM FF_REF_PRODUCER WHERE FORM_ID = ?",
        (form_id,),
    )

    bom_fields = ("YIELD", "CONVERSION_COST_INR", "PACKAGING_COST_INR", "FREIGHT_COST_INR")
    missing: list[str] = []
    if not len(bom) or any(is_missing(bom.iloc[0][field]) for field in bom_fields):
        missing.append("BOM")
    if not len(form_api) or is_missing(form_api.iloc[0]["API_MG_PER_UNIT"]):
        missing.append("API formulation mapping/dose")
    if api_cost is None or not len(api_cost) or is_missing(api_cost.iloc[-1]["UNIT_VALUE_INR_KG"]):
        missing.append("API cost")
    if not len(ceiling):
        missing.append("ceiling price")
    return {
        "missing_a1": missing,
        "formulation": formulation,
        "bom": bom,
        "form_api": form_api,
        "api_id": api_id,
        "api_cost": api_cost,
        "ceiling": ceiling,
        "producers": producers,
    }


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return not bool(str(value).strip())


def run_context(as_of: date, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        rules=load_rules(),
        run=RunContext(
            run_id=f"{label}-{uuid.uuid4().hex[:10]}",
            as_of=as_of,
            dry_run=True,
        ),
    )


def finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def provenance_tags(
    signal: Any,
    dependency: Any,
    forecast: Any,
    input_status: dict[str, Any] | None = None,
) -> dict[str, str]:
    tags: dict[str, str] = {}
    if input_status is not None:
        formulation = input_status["formulation"]
        if len(formulation):
            tags["reference_formulation"] = str(formulation.iloc[0]["IS_PROXY"])
        form_api = input_status["form_api"]
        if len(form_api):
            tags["API_formulation_mapping"] = str(form_api.iloc[0]["IS_PROXY"])
    if signal is not None:
        tags.update(
            {
                f"A1.{name}": tag.value
                for name, tag in signal.data_quality.tags.items()
            }
        )
    if dependency is not None and dependency.data_quality is not None:
        tags.update(
            {
                f"A2.{name}": tag.value
                for name, tag in dependency.data_quality.tags.items()
            }
        )
    if forecast is not None:
        for name, fact in forecast.cause_facts.items():
            if isinstance(fact, dict) and fact.get("tag") in {"REAL", "PROXY", "SYNTH"}:
                tags[f"A3.{name}"] = fact["tag"]
    return dict(sorted(tags.items()))


def print_counts(title: str, bands: list[str]) -> None:
    counts = Counter(bands)
    print(title)
    print("  " + "  ".join(f"{band}: {counts[band]}" for band in BANDS))


def print_summary(rows: list[dict[str, Any]]) -> None:
    a1_bands = [row["A1 band"] for row in rows if row["A1 band"] in BANDS]
    a3_bands = [row["A3 band"] for row in rows if row["A3 band"] in BANDS]
    print_counts("A1 band counts:", a1_bands)
    print_counts("A3 band counts:", a3_bands)

    scored = [row for row in rows if row["A1 band"] in BANDS and row["A3 band"] in BANDS]
    print("\nA1 band (rows) vs A3 band (columns):")
    print("A1\\A3  " + "  ".join(f"{band:>5}" for band in BANDS))
    for a1_band in BANDS:
        cells = [
            sum(row["A1 band"] == a1_band and row["A3 band"] == a3_band for row in scored)
            for a3_band in BANDS
        ]
        print(f"{a1_band:<6} " + "  ".join(f"{count:>5}" for count in cells))

    rank = {"GREEN": 0, "AMBER": 1, "RED": 2}
    changed = [
        row
        for row in scored
        if row["A1 band"] != row["A3 band"]
    ]
    changed.sort(
        key=lambda row: (
            -abs(rank[row["A3 band"]] - rank[row["A1 band"]]),
            -float(row["A3 risk score"] or 0.0),
            row["FORM_ID"],
        )
    )
    print(f"\nChanged band from A1 to A3: {len(changed)} of {len(scored)} scored formulations.")
    print("Five largest changes (band-step distance; ties by descending A3 risk):")
    for row in changed[:5]:
        distance = abs(rank[row["A3 band"]] - rank[row["A1 band"]])
        print(
            f"  {row['FORM_ID']}: {row['A1 band']} -> {row['A3 band']} "
            f"(distance {distance}, A3 risk {row['A3 risk score']})"
        )


def batch_run(db: Any, targets: list[dict[str, str]]) -> list[dict[str, Any]]:
    ids = [row["FORM_ID"] for row in targets]
    rules = load_rules()
    ctx = SimpleNamespace(
        db=db,
        rules=rules,
        run=RunContext(
            run_id=f"batch35-{uuid.uuid4().hex[:10]}",
            formulation_ids=ids,
            as_of=AS_OF,
            dry_run=True,
        ),
    )
    input_status = {fid: inspect_inputs(db, fid, AS_OF) for fid in ids}
    failures: dict[str, list[str]] = {fid: [] for fid in ids}
    for fid, status in input_status.items():
        failures[fid].extend(f"NOT FOUND: {item}" for item in status["missing_a1"])

    signals: dict[str, Any] = {}
    dependencies: dict[str, Any] = {}
    forecasts: dict[str, Any] = {}
    try:
        signals = {signal.formulation_id: signal for signal in a1.run(ids, ctx)}
    except Exception as exc:
        for fid in ids:
            failures[fid].append(f"A1 failed: {type(exc).__name__}: {exc}")

    try:
        dependencies = {
            profile.formulation_id: profile
            for profile in a2.run(ids, ctx, engine="networkx")
        }
    except Exception as exc:
        for fid in ids:
            failures[fid].append(f"A2 failed: {type(exc).__name__}: {exc}")

    eligible = [
        fid
        for fid in ids
        if fid in signals
        and fid in dependencies
        and not input_status[fid]["missing_a1"]
        and finite_number(signals[fid].headroom_pct)
    ]
    if eligible:
        try:
            forecasts = {
                forecast.formulation_id: forecast
                for forecast in a3.run(
                    [signals[fid] for fid in eligible],
                    [dependencies[fid] for fid in eligible],
                    ctx,
                )
            }
        except Exception as exc:
            for fid in eligible:
                failures[fid].append(f"A3 failed: {type(exc).__name__}: {exc}")

    rows: list[dict[str, Any]] = []
    for target in targets:
        fid = target["FORM_ID"]
        status = input_status[fid]
        signal = signals.get(fid)
        dependency = dependencies.get(fid)
        forecast = forecasts.get(fid)
        has_a1 = (
            signal is not None
            and not status["missing_a1"]
            and finite_number(signal.headroom_pct)
        )
        if signal is not None and not has_a1 and not status["missing_a1"]:
            failures[fid].append("A1 returned a non-finite headroom")
        if has_a1 and dependency is not None and forecast is None and not any(
            item.startswith("A3 failed:") for item in failures[fid]
        ):
            failures[fid].append("A3 returned no forecast")
        if dependency is None and not any(item.startswith("A2 failed:") for item in failures[fid]):
            failures[fid].append("A2 returned no dependency profile")

        rows.append(
            {
                "FORM_ID": fid,
                "generic name": target["GENERIC"],
                "headroom": float(signal.headroom_pct) if has_a1 else "",
                "A1 band": signal.band.value if has_a1 else "",
                "n_producers_min": dependency.n_producers_min if dependency else "",
                "hhi": dependency.hhi if dependency else "",
                "concentration": dependency.concentration if dependency else "",
                "A3 risk score": forecast.exit_risk if forecast else "",
                "A3 band": forecast.exit_risk_band.value if forecast else "",
                "input provenance tags": json.dumps(
                    provenance_tags(signal, dependency, forecast, status),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(rows)} formulations).")
    print("Headroom and A3 risk score are fractions; e.g. 0.10 means 10%.")
    print_summary(rows)

    no_producer_evidence = [
        fid for fid, status in input_status.items() if not len(status["producers"])
    ]
    if no_producer_evidence:
        print(
            "\nNo producer evidence in the database for: "
            + ", ".join(sorted(no_producer_evidence))
        )
        print(
            "For this batch only, A2's returned zero-known-producer profile is its "
            "documented model behavior, not observed market-share data."
        )

    no_stock = []
    for fid, forecast in forecasts.items():
        facts = forecast.cause_facts
        stock = facts.get("usable_stock_qty", {}).get("value")
        materials = facts.get("hospital_materials", {}).get("value")
        if not materials or not finite_number(stock) or float(stock) <= 0:
            no_stock.append(fid)
    if no_stock:
        print(
            "Exposure omitted for these formulations (no usable hospital stock as of "
            f"{AS_OF.isoformat()}): " + ", ".join(sorted(no_stock))
        )
    else:
        print("No formulations lacked usable hospital stock; no exposure was omitted.")

    failed = {fid: reasons for fid, reasons in failures.items() if reasons}
    print("\nFormulations that failed or lacked required A1 inputs:")
    if not failed:
        print("  None.")
    else:
        for fid, reasons in sorted(failed.items()):
            print(f"  {fid}: {'; '.join(reasons)}")
    return rows


def run_historical_checks(db: Any) -> None:
    print(f"\nPre-revision check as of {HISTORICAL_AS_OF.isoformat()}:")
    for fid, expected_ceiling in HISTORICAL_TARGETS.items():
        status = inspect_inputs(db, fid, HISTORICAL_AS_OF)
        print(f"\n{fid}")
        ceiling = status["ceiling"]
        if len(ceiling):
            found_ceiling = float(ceiling.iloc[-1]["CEILING_PRICE"])
            print(
                f"  Ceiling price: {found_ceiling:.2f} INR "
                f"(effective {ceiling.iloc[-1]['EFFECTIVE_FROM']})"
            )
            if not math.isclose(found_ceiling, expected_ceiling, abs_tol=0.005):
                print(
                    f"  WARNING: repository value differs from requested "
                    f"{expected_ceiling:.2f}; using the observed dated value."
                )
        else:
            print(f"  NOT FOUND: ceiling price (expected {expected_ceiling:.2f} INR)")

        for label, found in (
            ("BOM", len(status["bom"]) > 0),
            ("API formulation mapping/dose", len(status["form_api"]) > 0),
            (
                "API cost",
                status["api_cost"] is not None and len(status["api_cost"]) > 0,
            ),
            ("producers", len(status["producers"]) > 0),
        ):
            print(f"  {label}: {'FOUND' if found else 'NOT FOUND'}")
        if status["api_id"] is not None and status["api_cost"] is not None and len(status["api_cost"]):
            cost_tag = status["api_cost"].iloc[-1]["IS_PROXY"]
            print(f"  API cost provenance: {cost_tag}")

        ctx = run_context(HISTORICAL_AS_OF, "pre-revision")
        ctx.db = db
        if status["missing_a1"]:
            print(
                "  A1 NOT RUN; missing required inputs: "
                + ", ".join(status["missing_a1"])
            )
            print("  Headroom: NOT FOUND")
            print("  A1 band: NOT FOUND")
            print("  A3 risk score/band: NOT RUN")
            continue

        try:
            [signal] = a1.run([fid], ctx)
        except Exception as exc:
            print(f"  A1 failed: {type(exc).__name__}: {exc}")
            print("  Headroom: NOT FOUND")
            print("  A1 band: NOT FOUND")
            print("  A3 risk score/band: NOT RUN")
            continue

        if not finite_number(signal.headroom_pct):
            print("  Headroom: NOT FOUND")
            print("  A1 band: NOT FOUND")
            print("  A3 risk score/band: NOT RUN (A1 headroom is non-finite)")
            continue
        print(f"  Headroom: {signal.headroom_pct:.6f} ({signal.headroom_pct:.2%})")
        print(f"  A1 band: {signal.band.value}")
        used_tags = {name: tag.value for name, tag in signal.data_quality.tags.items()}
        if len(status["form_api"]):
            used_tags["API_formulation_mapping"] = str(status["form_api"].iloc[0]["IS_PROXY"])
        if len(status["formulation"]):
            used_tags["reference_formulation"] = str(status["formulation"].iloc[0]["IS_PROXY"])
        print(
            "  Input provenance tags used: "
            + json.dumps(
                used_tags,
                sort_keys=True,
            )
        )

        if not len(status["producers"]):
            print("  A3 risk score/band: NOT RUN (producers NOT FOUND; no value substituted)")
            continue
        try:
            dependency = a2.run([fid], ctx, engine="networkx")[0]
            forecast = a3.run([signal], [dependency], ctx)[0]
        except Exception as exc:
            print(f"  A3 failed: {type(exc).__name__}: {exc}")
            print("  A3 risk score/band: NOT FOUND")
            continue
        print(f"  A3 risk score: {forecast.exit_risk}")
        print(f"  A3 band: {forecast.exit_risk_band.value}")
        print(
            "  Additional input provenance tags used: "
            + json.dumps(
                provenance_tags(signal, dependency, forecast, status),
                sort_keys=True,
            )
        )


def main() -> int:
    targets = load_targets()
    with get_db() as db:
        batch_run(db, targets)
        run_historical_checks(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
