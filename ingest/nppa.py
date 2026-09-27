"""Parse NPPA ceiling-price Gazette orders into FF_REF_FORMULATION and FF_REF_CEILING_PRICE (plan §5).

    python -m ingest.nppa --backend sqlite     # parse data/raw/, write data/seed/, load the DB

Inputs (data/raw/, gitignored; see NPPA_ORDERS for the source URLs):
  * the annual WPI revision orders of 25-Mar-2026 (S.O. 1575(E) NLEM 2022, S.O. 1581(E) NLEM 2015),
    bilingual Gazette PDFs; only the English table is read;
  * data/raw/nlem2022.pdf for level of care and therapeutic class (ingest/nlem.py);
  * data/raw/para19/*.pdf for Para-19 revisions (ingest/nppa_para19.py).
Outputs: data/seed/FF_REF_FORMULATION.csv, FF_REF_CEILING_PRICE.csv, targets.csv, para19_events.csv,
then an idempotent upsert of the two FF_REF tables. Every value comes from a source file; prices are
ceiling prices EXCLUDING GST, exactly as notified. Nothing is typed by hand.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents.ctx import REPO_ROOT, SQLITE_PATH, get_db, load_settings
from ingest import load_hana as lh
from ingest.nlem import NLEM_PDF, NlemEntry, clean, load_nlem, name_key

RAW = REPO_ROOT / "data" / "raw"
SEED = REPO_ROOT / "data" / "seed"

# (file in data/raw, source URL, NLEM vintage). The first one is required.
NPPA_ORDERS = (
    ("nppa_ceiling_nlem2022_2026-03-27.pdf",
     "https://nppa.gov.in/storage/uploads/tender/0fd8be342c35d5a46a13b281cadc5e4f.pdf", "NLEM 2022"),
    ("nppa_ceiling_nlem2015_2026-03-27.pdf",
     "https://nppa.gov.in/storage/uploads/tender/0d883ca9b600c8a13cec452376176fc4.pdf", "NLEM 2015"),
)
WPI_REVISION_PARA = "16"  # DPCO 2013 para 16: annual revision of ceiling prices by WPI

_DEVANAGARI = re.compile("[ऀ-ॿ]")
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), 1)}
_FORM_ALIASES = (  # abbreviation -> canonical dosage-form word
    (re.compile(r"\btabs?\b\.?|\btablets\b", re.I), "Tablet"),
    (re.compile(r"\bcaps?\b\.?|\bcapsules\b", re.I), "Capsule"),
    (re.compile(r"\binj\b\.?|\binjections\b", re.I), "Injection"),
    (re.compile(r"\bsusp\b\.?", re.I), "Suspension"),
    (re.compile(r"\bsyp\b\.?", re.I), "Syrup"),
)
_UNIT_TOKENS = {"mg": "mg", "g": "g", "gm": "g", "mcg": "mcg", "µg": "mcg", "ml": "mL", "l": "L", "iu": "IU",
                "%": "%"}


@dataclass(frozen=True)
class RawRow:
    """One row of the English table, as printed."""

    sl_no: int
    medicine: str
    form_strength: str
    unit: str
    ceiling_price: Decimal
    prev_so: str
    prev_date: str


@dataclass(frozen=True)
class OrderHeader:
    so_number: str       # "1575(E)"
    order_date: date     # date of the order
    effective_from: date  # w.e.f. date from the table header (falls back to order_date)

    @property
    def citation(self) -> str:
        return f"S.O. {self.so_number} dated {self.order_date:%d.%m.%Y}"


# ---------------------------------------------------------------- normalisation


_SMALL_WORDS = {"for", "of", "in", "with", "and", "or", "use", "as", "per"}


def _cap_word(run: str) -> str:
    """Capitalise one alphabetic run; keep short all-caps tokens (BCG, ORS, TD, A, B) as printed."""
    if run.isupper() and len(run) <= 4:
        return run
    return run[:1].upper() + run[1:].lower()


def normalise_generic(name: str) -> str:
    """Title case every word (also inside brackets / after '-' or '/'), single spaces, ' + ' joins."""
    s = clean(name).rstrip("*").strip()
    s = re.sub(r"\s*\+\s*", " + ", s)
    return re.sub(r"[A-Za-z]+", lambda m: _cap_word(m.group(0)), s)


def normalise_dosage_form(form: str) -> str:
    """'Powder for injection' -> 'Powder for Injection', 'Oral liquid' -> 'Oral Liquid'."""
    words = form.split(" ")
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        out.append(low if i and low in _SMALL_WORDS else re.sub(r"[A-Za-z]+", lambda m: _cap_word(m.group(0)), w, count=1))
    return " ".join(out)


def normalise_strength(s: str) -> str:
    """'50mg' -> '50 mg', '5 ML' -> '5 mL', '1gm' -> '1 g'; collapse spaces around '/' and '+'."""
    def unit(m: re.Match) -> str:
        return f"{m.group(1)} {_UNIT_TOKENS[m.group(2).lower()]}"
    s = re.sub(r"(\d(?:[\d.,]*\d)?)\s*(mcg|µg|mg|gm|g|ml|l|iu|%)(?![a-z])", unit, s, flags=re.I)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"/(ml|l)\b", lambda m: "/" + _UNIT_TOKENS[m.group(1).lower()], s, flags=re.I)
    s = re.sub(r"\s*\+\s*", " + ", s)
    return " ".join(s.split())


def split_form_strength(text: str) -> tuple[str, str | None]:
    """'Powder for Injection 250 mg' -> ('Powder for Injection', '250 mg'). Abbreviations expanded."""
    s = clean(text)
    for pattern, full in _FORM_ALIASES:
        s = pattern.sub(full, s)
    m = re.search(r"\s*\(?\d", s)
    if not m or m.start() == 0:
        return (normalise_dosage_form(s), None) if not m else ("", normalise_strength(s))
    form, strength = s[:m.start()].strip(" -,"), s[m.start():].strip()
    if form.count("(") > form.count(")") and strength.endswith(")"):  # '(Solution ... 5 mg/ml)'
        form, strength = form + ")", strength[:-1].strip()
    return normalise_dosage_form(form), normalise_strength(strength)


def normalise_unit(unit: str) -> str:
    """'1 ML' -> '1 mL', '1 tablet' -> '1 Tablet', 'Per ML' -> 'Per mL'."""
    s = normalise_strength(clean(unit))
    s = re.sub(r"\b(ml|gm)\b", lambda m: _UNIT_TOKENS[m.group(1).lower()], s, flags=re.I)
    keep = set(_UNIT_TOKENS.values())
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0) if m.group(0) in keep else _cap_word(m.group(0)), s)


def make_form_id(generic: str, dosage_form: str, strength: str | None, unit: str) -> str:
    """Stable id from the normalised description: '<GENERIC-SLUG>-<8 hex>' (<= 33 chars)."""
    slug = re.sub(r"[^A-Z0-9]+", "-", generic.upper()).strip("-")[:24].rstrip("-")
    digest = hashlib.sha1("|".join((name_key(generic), dosage_form.lower(), (strength or "").lower(),
                                    unit.lower())).encode()).hexdigest()[:8].upper()
    return f"{slug}-{digest}"


# ---------------------------------------------------------------- parsing


def parse_date_words(text: str) -> date | None:
    """'the 25th March, 2026' -> date(2026, 3, 25)."""
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", text)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_header(english_text: str, header_cells: list[str]) -> OrderHeader:
    """S.O. number and order date from the English preamble; w.e.f. from the price column header."""
    m = re.search(r"New Delhi,\s*the\s+(.{5,30}?\d{4})\s*S\.O\.\s*(\d+)\s*\(E\)", english_text, re.S)
    if not m:
        raise ValueError("English order header (New Delhi, the <date> S.O. <n>(E)) not found")
    order_date = parse_date_words(m.group(1))
    if order_date is None:
        raise ValueError(f"cannot parse order date {m.group(1)!r}")
    wef = None
    for cell in header_cells:
        if w := re.search(r"w\.?e\.?f\.?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", cell or "", re.I):
            wef = date(int(w.group(3)), int(w.group(2)), int(w.group(1)))
    return OrderHeader(f"{m.group(2)}(E)", order_date, wef or order_date)


def parse_row(cells: list[str | None]) -> RawRow | None:
    """A data row of the English table (7 columns), or None for headers / Hindi rows."""
    vals = [c or "" for c in cells]
    if len(vals) < 7 or _DEVANAGARI.search(" ".join(vals)) or not re.fullmatch(r"\d+\.?", vals[0].strip()):
        return None
    price = vals[4].replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", price):
        raise ValueError(f"Sl. {vals[0]}: ceiling price {vals[4]!r} is not a number")
    return RawRow(int(vals[0].strip().rstrip(".")), clean(vals[1]), clean(vals[2]), clean(vals[3]),
                  Decimal(price), clean(vals[5]), clean(vals[6]))


def read_order(path: Path) -> tuple[OrderHeader, list[RawRow]]:
    """Read one Gazette PDF: header + all English table rows (Sl. numbers must be 1..n, no gaps)."""
    import pdfplumber

    rows: list[RawRow] = []
    english, header_cells = "", []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not english and re.search(r"S\.O\.\s*\d+\s*\(E\)\.\s*[—-]", text):
                english = text[text.find("MINISTRY"):] if "MINISTRY" in text else text
            for table in page.extract_tables():
                for cells in table:
                    if cells and (cells[0] or "").strip().startswith("Sl"):
                        header_cells = [c or "" for c in cells]
                    if row := parse_row(cells):
                        rows.append(row)
    header = parse_header(english, header_cells)
    numbers = [r.sl_no for r in rows]
    if numbers != list(range(1, len(rows) + 1)):
        raise ValueError(f"{path.name}: serial numbers are not 1..{len(rows)} (missing or split rows)")
    return header, rows


@dataclass(frozen=True)
class Formulation:
    form_id: str
    generic: str
    dosage_form: str
    strength: str | None
    unit: str


def to_formulation(row: RawRow) -> Formulation:
    generic = normalise_generic(row.medicine)
    form, strength = split_form_strength(row.form_strength)
    unit = normalise_unit(row.unit)
    if not form and not re.search(r"\d", unit):  # combi packs: strength '1 Tablet 100 mg (A) + ...'
        form = normalise_dosage_form(unit)
    return Formulation(make_form_id(generic, form, strength, unit), generic, form, strength, unit)


def _fetched_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provenance(source: str, url: str, path: Path) -> dict[str, str]:
    return {"SOURCE": source, "SOURCE_URL": url, "FETCHED_AT": _fetched_at(path), "IS_PROXY": "REAL"}


def nlem_match(generic: str, nlem: dict[str, NlemEntry]) -> NlemEntry | None:
    """Exact key match first, then the NLEM medicine whose key is a prefix of the NPPA name
    (e.g. 'Lithium Carbonate' -> NLEM 'Lithium'). Combinations only match combinations."""
    key = name_key(generic)
    if key in nlem:
        return nlem[key]
    if "+" in generic:
        return None
    candidates = [k for k in nlem if len(k) >= 5 and key.startswith(k)]
    return nlem[max(candidates, key=len)] if candidates else None


def build_reference(orders: list[tuple[Path, str, str]], nlem: dict[str, NlemEntry]
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, int], str]]:
    """Formulation and ceiling-price rows for every order; plus (S.O., Sl. No.) -> FORM_ID for Para-19 links."""
    formulations: dict[str, dict[str, Any]] = {}
    prices: list[dict[str, Any]] = []
    by_sl: dict[tuple[str, int], str] = {}
    for path, url, vintage in orders:
        header, rows = read_order(path)
        prov = _provenance(f"NPPA {header.citation} ({vintage} list)", url, path)
        for row in rows:
            f = to_formulation(row)
            if f.form_id in formulations and formulations[f.form_id]["GENERIC"] != f.generic:
                raise ValueError(f"FORM_ID collision for {f.form_id}")
            n = nlem_match(f.generic, nlem)
            nlem_note = " + NLEM 2022 (CDSCO)" if n else ""
            formulations.setdefault(f.form_id, {
                "FORM_ID": f.form_id, "GENERIC": f.generic, "DOSAGE_FORM": f.dosage_form,
                "STRENGTH": f.strength or "", "UNIT": f.unit, "NLEM_LEVEL": n.level if n else "",
                "THERAPEUTIC_CLASS": n.therapeutic_class if n else "",
                **prov, "SOURCE": prov["SOURCE"] + nlem_note})
            prices.append({"FORM_ID": f.form_id, "EFFECTIVE_FROM": header.effective_from.isoformat(),
                           "SO_NUMBER": header.citation, "CEILING_PRICE": str(row.ceiling_price),
                           "PARA": WPI_REVISION_PARA, "GST_RATE": "", **prov})
            by_sl[(header.so_number, row.sl_no)] = f.form_id
    return list(formulations.values()), prices, by_sl


# ---------------------------------------------------------------- target list (plan §1)

TARGET_COLUMNS = ["FORM_ID", "GENERIC", "DOSAGE_FORM", "STRENGTH", "UNIT", "CEILING_PRICE", "CEILING_SO",
                  "NLEM_LEVEL", "THERAPEUTIC_CLASS", "CATEGORY", "PARA19", "WHY_CHOSEN"]
_P19_2024 = "Para-19 Oct-2024 (§4.5, verify)"
_P19_2019 = "Para-19 Dec-2019 (§4.5 example, verify)"
_P19_2026 = "Para-19 Jun-2026 (order parsed)"
# (category, generic name_key prefix, dosage-form regex, strength regex, reason). Each spec must match
# at least one parsed formulation, otherwise the build fails: targets are never invented.
TARGET_SPEC = (
    ("Para-19 / old antibiotic, injectable", "benzylpenicillin", r"^Powder for Injection$", r"^10 Lac", _P19_2024),
    ("Para-19 / injectable", "atropine", r"^Injection$", r"^0\.6 mg/mL$", _P19_2024),
    ("Para-19 / TB, injectable", "streptomycin", r"^Powder for Injection$", r"^750 mg$", _P19_2024),
    ("Para-19 / TB, injectable", "streptomycin", r"^Powder for Injection$", r"^1000 mg$", _P19_2024),
    ("Para-19 / respiratory", "salbutamol", r"^Tablet$", r"^2 mg$", _P19_2024),
    ("Para-19 / respiratory", "salbutamol", r"^Tablet$", r"^4 mg$", _P19_2024),
    ("Para-19 / respiratory", "salbutamol", r"^Respirator Solution", r"^5 mg/mL$", _P19_2024),
    ("Para-19 / ophthalmic", "pilocarpine", r"^Drops$", r"^2 %$", _P19_2024),
    ("Para-19 / old antibiotic", "cefadroxil", r"^Tablet$", r"^500 mg$", _P19_2024),
    ("Para-19 / injectable", "desferrioxamine", r"^Powder for Injection$", r"^500 mg$", _P19_2024),
    ("Para-19 / psychiatric", "lithium", r"^Tablet$", r"^300 mg$", _P19_2024),
    ("Para-19 / vaccine", "bcgvaccine", r"^As Licensed$", r"", f"{_P19_2019}; {_P19_2026}"),
    ("Para-19 / anti-malarial", "chloroquine", r"^Tablet$", r"^150 mg$", _P19_2019),
    ("Para-19 / anti-leprosy", "dapsone", r"^Tablet$", r"^100 mg$", _P19_2019),
    ("Para-19 / old antibiotic", "metronidazole", r"^Tablet$", r"^400 mg$", _P19_2019),
    ("Para-19 / vitamin", "ascorbicacid", r"^Tablet$", r"^500 mg$", _P19_2019),
    ("Para-19 / vaccine", "measlesrubellavaccine", r"^As Licensed$", r"", _P19_2026),
    ("Para-19 / vaccine", "measlesvaccine", r"^As Licensed$", r"", _P19_2026),
    ("Para-19 / injectable", "antitetanusimmunoglobulin", r"^As Licensed$", r"250 IU", _P19_2026),
    ("Para-19 / injectable", "antitetanusimmunoglobulin", r"^As Licensed$", r"500 IU", _P19_2026),
    ("Para-19 / oncology injectable", "carboplatin", r"^Injection$", r"^10 mg/mL$", _P19_2026),
    ("Para-19 / oncology injectable", "cisplatin", r"^Injection$", r"^1 mg/mL$", _P19_2026),
    ("Old antibiotic", "amoxicillin", r"^Capsule$", r"^500 mg$", "first-line oral antibiotic, low price"),
    ("Old antibiotic, injectable", "ampicillin", r"^Powder for Injection$", r"^500 mg$", "old beta-lactam injection"),
    ("Old antibiotic, injectable", "gentamicin", r"^Injection$", r"^40 mg/mL \(2 mL\)$", "old aminoglycoside"),
    ("Old antibiotic", "cotrimoxazole", r"^Tablet$", r"^400 mg \(A\) \+ 80 mg", "old antibiotic, very low price"),
    ("Old antibiotic", "doxycycline", r"^Capsule$", r"^100 mg$", "old antibiotic"),
    ("Injectable", "adrenaline", r"^Injection$", r"^1 mg/mL$", "emergency injectable, vital"),
    ("Injectable", "oxytocin", r"^Injection$", r"^10 IU/mL$", "obstetric injectable, vital"),
    ("Injectable", "heparin", r"^Injection$", r"^5000 IU/mL$", "anticoagulant injectable, vital"),
    ("TB", "isoniazid", r"^Tablet$", r"^300 mg$", "TB programme drug, very low price"),
    ("TB", "rifampicin", r"^Capsule$", r"^450 mg$", "TB programme drug"),
    ("TB", "pyrazinamide", r"^Tablet$", r"^500 mg$", "TB programme drug"),
    ("Anti-malarial", "primaquine", r"^Tablet$", r"^7\.5 mg$", "anti-malarial, low price"),
    ("Anti-malarial, injectable", "quinine", r"^Injection$", r"^300 mg/mL$", "severe malaria injectable"),
    ("Cardiac", "digoxin", r"^Tablet$", r"^0\.25 mg$", "old cardiac glycoside, low price"),
    ("Cardiac", "amlodipine", r"^Tablet$", r"^5 mg$", "high-volume cardiac, low price"),
    ("Cardiac", "isosorbidedinitrate", r"^Tablet$", r"^5 mg$", "anti-anginal, very low price"),
    ("Cardiac, injectable", "furosemide", r"^Injection$", r"^10 mg/mL$", "loop diuretic injectable"),
    ("Respiratory", "budesonide", r"^Respirator Solution", r"^0\.5 mg/mL$", "nebulised steroid"),
)


def select_targets(formulations: list[dict[str, Any]], prices: list[dict[str, Any]],
                   events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """30-40 target formulations from TARGET_SPEC; every row exists in the parsed NPPA data."""
    latest: dict[str, dict[str, Any]] = {}
    for p in sorted(prices, key=lambda p: p["EFFECTIVE_FROM"]):
        latest[p["FORM_ID"]] = p
    out, seen = [], set()
    for category, key, form_re, strength_re, reason in TARGET_SPEC:
        hits = [f for f in formulations if name_key(f["GENERIC"]).startswith(key)
                and re.search(form_re, f["DOSAGE_FORM"]) and re.search(strength_re, f["STRENGTH"] or "")]
        if not hits:
            raise ValueError(f"target spec matched nothing in the parsed data: {key} / {form_re} / {strength_re}")
        for f in hits:
            if f["FORM_ID"] in seen:
                continue
            seen.add(f["FORM_ID"])
            p = latest[f["FORM_ID"]]
            why = (f"{category}: {reason}. NLEM {f['NLEM_LEVEL'] or 'n/a'}; ceiling Rs {p['CEILING_PRICE']} "
                   f"per {f['UNIT']} excl. GST ({p['SO_NUMBER']})")
            out.append({"FORM_ID": f["FORM_ID"], "GENERIC": f["GENERIC"], "DOSAGE_FORM": f["DOSAGE_FORM"],
                        "STRENGTH": f["STRENGTH"], "UNIT": f["UNIT"], "CEILING_PRICE": p["CEILING_PRICE"],
                        "CEILING_SO": p["SO_NUMBER"], "NLEM_LEVEL": f["NLEM_LEVEL"],
                        "THERAPEUTIC_CLASS": f["THERAPEUTIC_CLASS"], "CATEGORY": category,
                        "PARA19": "yes" if "Para-19" in reason else "no", "WHY_CHOSEN": why})
    if not 30 <= len(out) <= 40:
        raise ValueError(f"target list has {len(out)} rows; plan §1 wants 30-40")
    return out


# ---------------------------------------------------------------- CSV + DB


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = columns or list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def prune_stale(db: Any, form_ids: set[str]) -> int:
    """Delete NPPA-sourced rows whose FORM_ID this run no longer produces (e.g. after a parser fix).
    Only rows with SOURCE 'NPPA S.O. ...' are touched; fixtures and other sources are left alone."""
    removed = 0
    for table in ("FF_REF_CEILING_PRICE", "FF_REF_FORMULATION"):
        existing = db.query(f"SELECT DISTINCT FORM_ID FROM \"{table}\" WHERE SOURCE LIKE 'NPPA S.O.%'")
        stale = [(fid,) for fid in existing["FORM_ID"] if fid not in form_ids]
        if stale:
            removed += db.executemany(f'DELETE FROM "{table}" WHERE FORM_ID = ? AND SOURCE LIKE \'NPPA S.O.%\'',
                                      stale)
    return removed


def available_orders() -> list[tuple[Path, str, str]]:
    found = [(RAW / name, url, vintage) for name, url, vintage in NPPA_ORDERS if (RAW / name).exists()]
    if not found or found[0][0].name != NPPA_ORDERS[0][0]:
        raise FileNotFoundError(f"missing data/raw/{NPPA_ORDERS[0][0]} (download: {NPPA_ORDERS[0][1]})")
    return found


def run(backend: str | None, sqlite_path: str) -> dict[str, int]:
    from ingest import nppa_para19

    if not NLEM_PDF.exists():
        raise FileNotFoundError("missing data/raw/nlem2022.pdf")
    nlem = load_nlem()
    formulations, prices, by_sl = build_reference(available_orders(), nlem)
    p19_prices, events = nppa_para19.build(by_sl, {p["FORM_ID"]: p for p in prices})
    prices += p19_prices
    target_rows = select_targets(formulations, prices, events)

    write_csv(SEED / "FF_REF_FORMULATION.csv", formulations)
    write_csv(SEED / "FF_REF_CEILING_PRICE.csv", prices)
    write_csv(SEED / "para19_events.csv", events, nppa_para19.EVENT_COLUMNS)
    write_csv(SEED / "targets.csv", target_rows, TARGET_COLUMNS)

    counts = {"formulations": len(formulations), "ceiling_prices": len(prices), "para19_events": len(events),
              "targets": len(target_rows), "nlem_matched": sum(1 for f in formulations if f["NLEM_LEVEL"])}
    if backend:
        settings = dataclasses.replace(load_settings(), db_backend=backend)
        tables = lh.schema_tables()
        with get_db(settings, sqlite_path=sqlite_path) as db:
            lh.create_objects(db)
            lh.upsert_rows(db, tables["FF_REF_FORMULATION"], formulations)
            lh.upsert_rows(db, tables["FF_REF_CEILING_PRICE"], prices)
            prune_stale(db, {f["FORM_ID"] for f in formulations})
            for t in ("FF_REF_FORMULATION", "FF_REF_CEILING_PRICE"):
                counts[f"db_{t}"] = int(db.query(f'SELECT COUNT(*) FROM "{t}"').iat[0, 0])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.nppa", description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=("hana", "sqlite"), help="also load the DB (default: only write seeds)")
    ap.add_argument("--sqlite-path", default=str(SQLITE_PATH))
    args = ap.parse_args(argv)
    try:
        counts = run(args.backend, args.sqlite_path)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {lh.scrub(exc)}", file=sys.stderr)
        return 1
    print(" | ".join(f"{k}={v}" for k, v in counts.items()))
    with (SEED / "targets.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print("sample targets:")
    for r in rows[:5]:
        print(f"  {r['FORM_ID']:<34} {r['GENERIC'][:28]:<28} {r['DOSAGE_FORM'][:22]:<22} "
              f"{r['STRENGTH'][:18]:<18} Rs {r['CEILING_PRICE']:>8}  {r['WHY_CHOSEN'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
