"""NPPA Para-19 (DPCO 2013, extraordinary circumstances) price revisions: backtest labels (plan §4.5).

    python -m ingest.nppa_para19      # parse data/raw/para19/*.pdf and print the events

Every order PDF in data/raw/para19/ is parsed (English table only). Each row cites the WPI order row it
revises ("1575(E) Sl. No. 92"), so the event links to a FORM_ID without name guessing, and the % change
is new ceiling / ceiling in that cited row - 1. Parsed events are REAL (VERIFY=false) and the new price
also becomes a PARA 19 row in FF_REF_CEILING_PRICE.
The §4.5 events we have no order PDF for (Oct-2024, Dec-2019) are listed by molecule only, with empty
dates and VERIFY=true: they must be confirmed against the NPPA orders before use.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from agents.ctx import REPO_ROOT
from ingest.nlem import clean

PARA19_DIR = REPO_ROOT / "data" / "raw" / "para19"
PARA19_URLS = {  # file in data/raw/para19 -> NPPA download URL
    "para19_2026-06-12_bcg_mr_measles.pdf":
        "https://nppa.gov.in/storage/uploads/tender/bcg-vaccine-6bc1c11c39cca0a0e8994f99b99a5e1d.pdf",
    "para19_2026-06-12_anti_tetanus.pdf":
        "https://nppa.gov.in/storage/uploads/tender/anti-tetanus-8787c4422856beb6644c1da23d7fd15b.pdf",
    "para19_2026-06-12_carboplatin_cisplatin.pdf":
        "https://nppa.gov.in/storage/uploads/tender/cisplatin-aa1ee6fd3965d662b8f4bdb981783485.pdf",
}
EVENT_COLUMNS = ["FORM_ID", "MOLECULE", "FORMULATION", "EVENT_GROUP", "ORDER_DATE", "OLD_CEILING",
                 "NEW_CEILING", "PCT_CHANGE", "SO_NUMBER", "SOURCE_URL", "VERIFY", "NOTE"]

# §4.5 events without an order PDF in data/raw/para19/: molecule names only, to be confirmed.
UNVERIFIED_45 = (
    ("2024-10", "https://www.pib.gov.in/PressReleseDetailm.aspx?PRID=2064747",
     "§4.5: ~11 formulations of 8 drugs, ~+50%. Confirm S.O. no., date, formulations and % from the NPPA order",
     ("Benzylpenicillin", "Atropine", "Streptomycin", "Salbutamol", "Pilocarpine", "Cefadroxil",
      "Desferrioxamine", "Lithium carbonate")),
    ("2019-12", "",
     "§4.5: ~21 formulations (examples only). Confirm the full list, S.O. no., date and % from the NPPA order",
     ("BCG vaccine", "Chloroquine", "Dapsone", "Metronidazole", "Ascorbic acid (Vitamin C)")),
)

_DEVANAGARI = re.compile("[ऀ-ॿ]")
_REF = re.compile(r"(\d+)\s*\(E\)[\s\S]*?Sl\.?\s*No\.?\s*(\d+)", re.I)


@dataclass(frozen=True)
class Para19Row:
    medicine: str
    form_strength: str
    new_price: Decimal
    ref_so: str      # cited WPI order, "1575(E)"
    ref_sl: int      # row in that order


@dataclass(frozen=True)
class Para19Order:
    so_number: str
    order_date: date
    rows: tuple[Para19Row, ...]

    @property
    def citation(self) -> str:
        return f"S.O. {self.so_number} dated {self.order_date:%d.%m.%Y}"


def parse_table(table: list[list[str | None]]) -> list[Para19Row]:
    """English Para-19 table: new price = the 'revised' price column; cited row = '<n>(E) ... Sl. No. <m>'."""
    header: list[str] | None = None
    out = []
    for cells in table:
        vals = [c or "" for c in cells]
        if _DEVANAGARI.search(" ".join(vals)):
            continue
        if vals and vals[0].strip().startswith("Sl"):
            header = [clean(v).lower() for v in vals]
            continue
        if header is None or not re.fullmatch(r"\d+\.?", vals[0].strip()):
            continue
        price_cols = [i for i, h in enumerate(header) if "revised" in h and "price" in h]
        if not price_cols:
            raise ValueError(f"no 'revised ... price' column in header {header}")
        ref = _REF.search(" ".join(vals[price_cols[-1] + 1:]))
        if not ref:
            raise ValueError(f"row {vals[0]}: no cited S.O./Sl. No. in {vals}")
        out.append(Para19Row(clean(vals[1]), clean(vals[2]), Decimal(vals[price_cols[-1]].replace(",", "").strip()),
                             f"{ref.group(1)}(E)", int(ref.group(2))))
    return out


def read_order(path: Path) -> Para19Order:
    import pdfplumber

    from ingest.nppa import parse_date_words

    so, order_date, rows = None, None, []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if so is None and (m := re.search(r"New Delhi,\s*the\s+(.{5,30}?\d{4})\s*S\.O\.\s*(\d+)\s*\(E\)", text, re.S)):
                order_date, so = parse_date_words(m.group(1)), f"{m.group(2)}(E)"
            for table in page.extract_tables():
                rows += parse_table(table)
    if not so or not order_date or not rows:
        raise ValueError(f"{path.name}: could not read S.O. number, date or table")
    return Para19Order(so, order_date, tuple(rows))


def _pct(new: Decimal, old: Decimal) -> str:
    return str(((new / old - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build(by_sl: dict[tuple[str, int], str], wpi_prices: dict[str, dict[str, Any]]
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(PARA 19 ceiling-price rows, events) from every order in data/raw/para19/ plus the unverified §4.5 list.

    `by_sl` maps (S.O., Sl. No.) of the parsed WPI orders to FORM_ID; `wpi_prices` maps FORM_ID to its
    FF_REF_CEILING_PRICE row from that order (the "old" price).
    """
    from ingest.nppa import _provenance, normalise_generic

    prices, events = [], []
    for path in sorted(PARA19_DIR.glob("*.pdf")) if PARA19_DIR.is_dir() else []:
        url = PARA19_URLS.get(path.name, "")
        order = read_order(path)
        prov = _provenance(f"NPPA {order.citation} (Para 19)", url, path)
        for row in order.rows:
            form_id = by_sl.get((row.ref_so, row.ref_sl), "")
            old = wpi_prices.get(form_id)
            verify, note = False, ""
            if not old:
                verify, note = True, f"cited {row.ref_so} Sl. No. {row.ref_sl} not found in the parsed WPI orders"
            elif not _same_molecule(row.medicine, form_id):
                verify, note = True, f"molecule {row.medicine!r} differs from the cited row {form_id}"
            events.append({
                "FORM_ID": form_id, "MOLECULE": normalise_generic(row.medicine), "FORMULATION": row.form_strength,
                "EVENT_GROUP": f"{order.order_date:%Y-%m}", "ORDER_DATE": order.order_date.isoformat(),
                "OLD_CEILING": old["CEILING_PRICE"] if old else "", "NEW_CEILING": str(row.new_price),
                "PCT_CHANGE": _pct(row.new_price, Decimal(old["CEILING_PRICE"])) if old else "",
                "SO_NUMBER": order.citation, "SOURCE_URL": url, "VERIFY": str(verify).lower(), "NOTE": note})
            if form_id and not verify:
                prices.append({"FORM_ID": form_id, "EFFECTIVE_FROM": order.order_date.isoformat(),
                               "SO_NUMBER": order.citation, "CEILING_PRICE": str(row.new_price), "PARA": "19",
                               "GST_RATE": "", **prov})
    for group, url, note, molecules in UNVERIFIED_45:
        for molecule in molecules:
            events.append({"FORM_ID": "", "MOLECULE": molecule, "FORMULATION": "", "EVENT_GROUP": group,
                           "ORDER_DATE": "", "OLD_CEILING": "", "NEW_CEILING": "", "PCT_CHANGE": "",
                           "SO_NUMBER": "", "SOURCE_URL": url, "VERIFY": "true", "NOTE": note})
    return prices, events


def _same_molecule(medicine: str, form_id: str) -> bool:
    """The FORM_ID slug starts with the generic name; compare the first word of the molecule."""
    first = re.sub(r"[^a-z0-9]", "", medicine.lower().split()[0]) if medicine.split() else ""
    return bool(first) and form_id.lower().replace("-", "").startswith(first[:6])


def main() -> int:
    if not PARA19_DIR.is_dir():
        print("no data/raw/para19/ directory: only the unverified §4.5 molecules would be listed")
        return 0
    for path in sorted(PARA19_DIR.glob("*.pdf")):
        order = read_order(path)
        print(f"{path.name}: {order.citation}")
        for r in order.rows:
            print(f"  {r.medicine:<30} {r.form_strength:<28} new Rs {r.new_price:>9}  cites {r.ref_so} Sl. {r.ref_sl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
