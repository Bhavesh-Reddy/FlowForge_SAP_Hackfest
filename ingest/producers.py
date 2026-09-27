"""Producers per formulation -> FF_REF_PRODUCER and FF_REF_MANUFACTURER (plan §4.2, §5).

    python -m ingest.producers      # needs data/raw/nsq/ (see ingest/nsq.py)

Evidence used here: a CDSCO NSQ result names the manufacturer of a batch of a formulation, which proves that
manufacturer makes it. Each (FORM_ID, manufacturer) pair keeps its first evidence in EVIDENCE_SOURCE.
This is a LOWER BOUND ("at least N producers"): firms that never had a failed sample are not seen, and
MARKET_SHARE is unknown (left empty). Other evidence sources in §5 (NPPA price notifications with company
names, CDSCO approvals, BPPI supplier lists) can be appended here later with their own EVIDENCE_SOURCE.
FF_REF_MANUFACTURER holds every manufacturer seen in the NSQ data (name and state parsed from the address).
"""
from __future__ import annotations

import sys
from typing import Any

from ingest.nsq import SEED_DIR, Manufacturer, build as build_nsq, write_csv


def build(alerts: list[dict[str, Any]], makers: dict[str, Manufacturer]) -> tuple[list[dict], list[dict]]:
    producers: dict[tuple[str, str], dict[str, Any]] = {}
    for a in alerts:  # alerts are sorted by month, so the first evidence is the earliest
        if not a["FORM_ID"] or not a["MANUFACTURER_ID"]:
            continue
        key = (a["FORM_ID"], a["MANUFACTURER_ID"])
        producers.setdefault(key, {
            "FORM_ID": a["FORM_ID"], "MANUFACTURER_ID": a["MANUFACTURER_ID"],
            "EVIDENCE_SOURCE": f"CDSCO NSQ {a['MONTH'][:7]} batch {a['BATCH']} ({a['ALERT_ID']})"[:200],
            "MARKET_SHARE": "", "SOURCE": "CDSCO NSQ public table (manufacturer of a tested batch)",
            "SOURCE_URL": a["SOURCE_URL"], "FETCHED_AT": a["FETCHED_AT"], "IS_PROXY": "REAL"})
    fetched = max((a["FETCHED_AT"] for a in alerts), default="")
    manufacturers = [{"ID": m.id, "NAME": m.name, "STATE": m.state, "LICENCE_NO": "",
                      "SOURCE": "CDSCO NSQ public table (manufacturer address)", "SOURCE_URL": alerts[0]["SOURCE_URL"],
                      "FETCHED_AT": fetched, "IS_PROXY": "REAL"} for m in sorted(makers.values(), key=lambda m: m.id)]
    return sorted(producers.values(), key=lambda p: (p["FORM_ID"], p["MANUFACTURER_ID"])), manufacturers


def main() -> int:
    alerts, makers = build_nsq()
    producers, manufacturers = build(alerts, makers)
    write_csv(SEED_DIR / "FF_REF_PRODUCER.csv", producers)
    write_csv(SEED_DIR / "FF_REF_MANUFACTURER.csv", manufacturers)
    forms = {p["FORM_ID"] for p in producers}
    print(f"FF_REF_PRODUCER={len(producers)} pairs over {len(forms)} formulations | "
          f"FF_REF_MANUFACTURER={len(manufacturers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
