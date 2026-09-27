"""Wholesale Price Index (Office of the Economic Adviser) -> FF_REF_WPI (plan §4.1, §5).

    python -m ingest.wpi            # parse data/raw/wpi/*, write data/seed/FF_REF_WPI.csv

Inputs (data/raw/wpi/, gitignored):
  wpi_2223_monthly_index_*.xlsx   2022-23 base, Apr-2023 onwards   (REAL)
  wpi_1112_monthly_index_*.xls    2011-12 base, Apr-2012 onwards   (optional; needs xlrd)
One continuous series per SERIES on the 2022-23 base: months from the 2022-23 file are REAL; earlier months
are the 2011-12 index x a link factor (mean ratio of the two bases over the first 12 overlapping months)
and are tagged PROXY. A1 reads SERIES 'MANUFACTURED_PRODUCTS'.
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from agents.ctx import REPO_ROOT

RAW = REPO_ROOT / "data" / "raw" / "wpi"
SEED = REPO_ROOT / "data" / "seed" / "FF_REF_WPI.csv"
URL_2223 = "https://eaindustry.nic.in/download_data_2223.asp"
URL_1112 = "https://eaindustry.nic.in/download_data_1112.asp"
SERIES = {  # WPI commodity code -> SERIES name
    "1300000000": "MANUFACTURED_PRODUCTS",
    "1310000000": "CHEMICALS",
    "1311000000": "PHARMACEUTICALS",
}
LINK_MONTHS = 12
_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                    "dec"), 1)}


def parse_month(label: object) -> date | None:
    """'Apr-23' / 'Apr-2023' (2022-23 file) or 'INDX042012' (2011-12 file) -> first day of month."""
    s = str(label).strip()
    if m := re.fullmatch(r"INDX(\d{2})(\d{4})", s):
        return date(int(m.group(2)), int(m.group(1)), 1)
    if m := re.fullmatch(r"([A-Za-z]{3})-(\d{2}|\d{4})", s):
        y = int(m.group(2))
        return date(y + 2000 if y < 100 else y, _MON[m.group(1).lower()], 1)
    return None


def read_series(df: pd.DataFrame) -> dict[str, dict[date, float]]:
    """{SERIES: {month: index}} from a raw sheet (header row 0; commodity code in the column named *code*)."""
    header = [str(h).strip() for h in df.iloc[0]]
    code_col = next(i for i, h in enumerate(header) if "code" in h.lower())
    months = {i: parse_month(h) for i, h in enumerate(header)}
    out: dict[str, dict[date, float]] = {}
    for _, row in df.iloc[1:].iterrows():
        code = str(row.iloc[code_col]).split(".")[0].strip()
        if code in SERIES:
            out[SERIES[code]] = {m: float(row.iloc[i]) for i, m in months.items()
                                 if m is not None and pd.notna(row.iloc[i])}
    return out


def splice(new: dict[date, float], old: dict[date, float]) -> list[tuple[date, float, str]]:
    """(month, index on the new base, tag). New-base months are REAL; linked older months are PROXY."""
    overlap = sorted(set(new) & set(old))[:LINK_MONTHS]
    rows = [(m, v, "REAL") for m, v in sorted(new.items())]
    if not overlap:
        return rows
    factor = sum(new[m] / old[m] for m in overlap) / len(overlap)
    first_new = min(new)
    rows += [(m, round(v * factor, 4), "PROXY") for m, v in old.items() if m < first_new]
    return sorted(rows)


def build(raw_dir: Path = RAW) -> list[dict[str, object]]:
    new_files = sorted(raw_dir.glob("wpi_2223_*.xls*"))
    if not new_files:
        raise FileNotFoundError(f"missing data/raw/wpi/wpi_2223_monthly_index_*.xlsx (download: {URL_2223})")
    new = read_series(pd.read_excel(new_files[-1], header=None))
    old: dict[str, dict[date, float]] = {}
    old_files = sorted(raw_dir.glob("wpi_1112_*.xls*"))
    if old_files:
        old = read_series(pd.read_excel(old_files[-1], header=None))
    missing = set(SERIES.values()) - set(new)
    if missing:
        raise ValueError(f"series not found in {new_files[-1].name}: {sorted(missing)}")
    rows = []
    for name in SERIES.values():
        for month, value, tag in splice(new[name], old.get(name, {})):
            src = new_files[-1] if tag == "REAL" else old_files[-1]
            note = "OEA WPI 2022-23 base" if tag == "REAL" else "OEA WPI 2011-12 base linked to 2022-23"
            rows.append({"MONTH": month.isoformat(), "SERIES": name, "INDEX_VALUE": round(value, 4),
                         "SOURCE": f"{note} ({src.name})", "SOURCE_URL": URL_2223 if tag == "REAL" else URL_1112,
                         "FETCHED_AT": pd.Timestamp(src.stat().st_mtime, unit="s").strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "IS_PROXY": tag})
    return rows


def main() -> int:
    rows = build()
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by.setdefault(r["SERIES"], []).append(r)
    for s, rs in by.items():
        print(f"{s:<22} {len(rs)} months {rs[0]['MONTH']}..{rs[-1]['MONTH']} "
              f"(REAL {sum(r['IS_PROXY'] == 'REAL' for r in rs)}, linked {sum(r['IS_PROXY'] == 'PROXY' for r in rs)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
