"""CDSCO "Not of Standard Quality" results -> FF_REF_NSQ_ALERT (plan §5, recall trace + quality signal).

    python -m ingest.nsq            # parse data/raw/nsq/*.json, write data/seed/FF_REF_NSQ_ALERT.csv

Source: the public NSQ table at https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug (CDSCO stopped the
monthly PDFs after Jun-2025). Each data/raw/nsq/nsq_<year>_<Mon>.json is that table's own JSON for one
reporting month (source 'All'). Every row is REAL. The product is matched to a FORM_ID only when generic
name, dosage form and (if printed) strength agree with exactly one NPPA formulation; otherwise FORM_ID stays
empty and the alert still counts at manufacturer level. Manufacturer name and state come from the address.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from agents.ctx import REPO_ROOT
from ingest.nlem import name_key

RAW = REPO_ROOT / "data" / "raw" / "nsq"
SEED_DIR = REPO_ROOT / "data" / "seed"
PORTAL_URL = "https://cdscoonline.gov.in/CDSCO/viewPublicNSQDrug"
_MON = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                    "DEC"), 1)}
_FORMS = (  # keyword in the NSQ product name -> keyword that must appear in the NPPA dosage form
    ("tablet", "tablet"), ("tab", "tablet"), ("capsule", "capsule"), ("cap", "capsule"),
    ("injection", "injection"), ("inj", "injection"), ("infusion", "injection"), ("syrup", "oral liquid"),
    ("suspension", "oral liquid"), ("oral liquid", "oral liquid"), ("drops", "drops"), ("cream", "cream"),
    ("ointment", "ointment"), ("inhaler", "inhalation"), ("respules", "respirator"), ("nebul", "respirator"),
)
_STATES = {
    "Andhra Pradesh": ("andhra pradesh",), "Assam": ("assam",), "Bihar": ("bihar",),
    "Chhattisgarh": ("chhattisgarh",), "Delhi": ("delhi",), "Goa": ("goa",), "Gujarat": ("gujarat",),
    "Haryana": ("haryana",), "Himachal Pradesh": ("himachal", "(h.p.)", " h.p.", "(hp)"),
    "Jammu & Kashmir": ("jammu", "kashmir"), "Jharkhand": ("jharkhand",), "Karnataka": ("karnataka",),
    "Kerala": ("kerala",), "Madhya Pradesh": ("madhya pradesh", "(m.p.)", " m.p."),
    "Maharashtra": ("maharashtra",), "Odisha": ("odisha", "orissa"), "Puducherry": ("puducherry", "pondicherry"),
    "Punjab": ("punjab",), "Rajasthan": ("rajasthan",), "Sikkim": ("sikkim",), "Tamil Nadu": ("tamil nadu",),
    "Telangana": ("telangana",), "Uttar Pradesh": ("uttar pradesh", "(u.p.)", " u.p."),
    "Uttarakhand": ("uttarakhand", "uttaranchal"), "West Bengal": ("west bengal",),
}
_NAME_STOP = re.compile(r",|#|\(|\s{2,}|\s(?:plot|vill(?:age)?|v\.?p\.?o|khasra|survey|sy\.|door|near|opp\.?|"
                        r"at\s|post|sector|ward|unit|shed|gat|no\.)\b|\s\d", re.I)
_SUFFIX = re.compile(r"\b(PVT|PRIVATE|LTD|LIMITED|LLP|INC|CO|COMPANY|INDIA|THE|M/S)\b")


@dataclass(frozen=True)
class Manufacturer:
    id: str
    name: str
    state: str


def read_json(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return json.loads(text)["aaData"]


def clean(s: object) -> str:
    return " ".join(str(s or "").replace("�", " ").split())


def parse_month(label: str) -> date:
    """'AUG-2026' -> date(2026, 8, 1)."""
    mon, year = clean(label).upper().split("-")
    return date(int(year), _MON[mon[:3]], 1)


def manufacturer(text: str) -> Manufacturer | None:
    """Name (before the address), a stable ID from the normalised name, and the state if it is printed."""
    full = clean(text)
    if not full:
        return None
    body = re.sub(r"^m/s\.?\s*", "", full, flags=re.I)
    m = _NAME_STOP.search(body)
    name = (body[:m.start()] if m and m.start() > 2 else body).strip(" .,-:")
    norm = " ".join(_SUFFIX.sub(" ", re.sub(r"[^A-Z0-9 ]", " ", name.upper())).split())
    if len(norm) < 3:
        return None
    slug = re.sub(r"[^A-Z0-9]+", "-", norm)[:24].strip("-")
    mid = f"MFR-{slug}-{hashlib.sha1(norm.encode()).hexdigest()[:6].upper()}"
    low = f" {full.lower()} "
    state = next((s for s, keys in _STATES.items() if any(k in low for k in keys)), "")
    return Manufacturer(mid, name[:200], state)


def _strengths(text: str) -> set[str]:
    return {f"{float(n):g} {u.lower()}" for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|iu|ml|%)", text, re.I)}


class FormMatcher:
    """Match an NSQ product name to one NPPA formulation (generic + dosage form + strength)."""

    def __init__(self, formulations: list[dict[str, str]]):
        self.by_key: dict[tuple[str, ...], list[dict[str, str]]] = {}
        for f in formulations:
            parts = tuple(sorted(k for k in (name_key(p) for p in f["GENERIC"].split("+")) if len(k) >= 5))
            if parts:
                self.by_key.setdefault(parts, []).append(f)

    def match(self, product: str) -> str:
        pkey = re.sub(r"[^a-z0-9]", "", product.lower())
        keys = [k for k in self.by_key if all(p in pkey for p in k)]
        if not keys:
            return ""
        best = max(len(k) for k in keys)                    # a combination beats its single components
        cands = [f for k in keys if len(k) == best for f in self.by_key[k]]
        low = product.lower()
        form = next((want for kw, want in _FORMS if re.search(rf"\b{kw}", low)), None)
        if form:
            cands = [f for f in cands if form in f["DOSAGE_FORM"].lower()]
        wanted = _strengths(product)
        if wanted:
            cands = [f for f in cands if wanted & _strengths(f.get("STRENGTH") or "")] or \
                    ([] if len(cands) > 1 else cands)
        return cands[0]["FORM_ID"] if len(cands) == 1 else ""


def build(raw_dir: Path = RAW, formulations: list[dict[str, str]] | None = None
          ) -> tuple[list[dict[str, Any]], dict[str, Manufacturer]]:
    """(FF_REF_NSQ_ALERT rows, manufacturers by ID). Duplicate reports of the same batch collapse to one row."""
    files = sorted(raw_dir.glob("nsq_*.json"))
    if not files:
        raise FileNotFoundError(f"no data/raw/nsq/nsq_*.json (source: {PORTAL_URL})")
    if formulations is None:
        with (SEED_DIR / "FF_REF_FORMULATION.csv").open(encoding="utf-8") as fh:
            formulations = list(csv.DictReader(fh))
    matcher = FormMatcher(formulations)
    rows: dict[str, dict[str, Any]] = {}
    makers: dict[str, Manufacturer] = {}
    for path in files:
        fetched = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in read_json(path):
            product, batch = clean(r.get("str_product_name")), clean(r.get("str_batch_no"))
            if not product or not batch:
                continue
            month = parse_month(r["dt_reporting_month_year"])
            mfr = manufacturer(r.get("str_manufactured_by"))
            if mfr:
                makers.setdefault(mfr.id, mfr)
            alert_id = "NSQ-" + hashlib.sha1(f"{month}|{product.upper()}|{batch.upper()}|{mfr.id if mfr else ''}"
                                             .encode()).hexdigest()[:12].upper()
            src = f"CDSCO NSQ ({clean(r.get('str_reporting_source'))}: {clean(r.get('str_reported_by_lab_or_state'))})"
            rows.setdefault(alert_id, {
                "ALERT_ID": alert_id, "MONTH": month.isoformat(), "DRUG": product[:300], "BATCH": batch[:60],
                "MANUFACTURER_ID": mfr.id if mfr else "", "FORM_ID": matcher.match(product),
                "REASON": clean(r.get("str_nsq_result"))[:500], "SOURCE": src[:120], "SOURCE_URL": PORTAL_URL,
                "FETCHED_AT": fetched, "IS_PROXY": "REAL"})
    return sorted(rows.values(), key=lambda x: (x["MONTH"], x["ALERT_ID"])), makers


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    rows, makers = build()
    write_csv(SEED_DIR / "FF_REF_NSQ_ALERT.csv", rows)
    print(f"FF_REF_NSQ_ALERT={len(rows)} ({sum(1 for r in rows if r['FORM_ID'])} matched to a FORM_ID) | "
          f"manufacturers={len(makers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
