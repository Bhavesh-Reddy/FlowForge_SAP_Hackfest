"""Parse the NLEM 2022 list (CDSCO PDF) into level of care and therapeutic class per medicine (plan §5).

    python -m ingest.nlem            # parse data/raw/nlem2022.pdf and print a summary

Only the main list (sections 1-27, before "Alphabetical List of Medicines") is read. Each medicine
row is `<code> <name> <levels> <forms>`, e.g. `4.2.1 | Atropine* | P,S,T | Injection 0.6 mg/mL`.
A medicine listed in several sections keeps its FIRST section as the therapeutic class.
Formulations are matched to NLEM medicines by `name_key` (see ingest/nppa.py).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.ctx import REPO_ROOT

NLEM_PDF = REPO_ROOT / "data" / "raw" / "nlem2022.pdf"
NLEM_URL = "https://cdsco.gov.in/opencms/resources/UploadCDSCOWeb/2018/UploadConsumer/nlem2022.pdf"

_DEVANAGARI = re.compile("[ऀ-ॿ]")
_CODE = re.compile(r"^\d+(?:\.\d+){1,3}$")
_SECTION = re.compile(r"^Section\s+(\d+)\s*\n(.+)", re.S)
_LEVELS = re.compile(r"^[PST](?:\s*,\s*[PST])*$")


@dataclass(frozen=True)
class NlemEntry:
    code: str
    name: str
    level: str          # "P,S,T" (primary, secondary, tertiary care)
    section_no: int
    therapeutic_class: str


def clean(text: str | None) -> str:
    """Collapse whitespace; re-join words broken across a cell line ('D\\nispersible', 'Anti-\\ntetanus')."""
    s = re.sub(r"(?<![A-Za-z])([A-Z])\n([a-z])", r"\1\2", text or "")
    s = " ".join(s.split())
    return re.sub(r"(\w)-\s+(\w)", r"\1-\2", s)


def name_key(name: str) -> str:
    """Matching key: lower case, no parentheses content, no footnote stars, letters+digits only.

    'Abacavir (A) + Lamivudine (B)' -> 'abacavirlamivudine';
    '5-amino salicylic Acid (Mesalazine/Mesalamine)' -> '5aminosalicylicacid'.
    """
    s = re.sub(r"\([^)]*\)", " ", name.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def parse_tables(tables_by_page: list[list[list[list[str | None]]]]) -> dict[str, NlemEntry]:
    """Pure parser over pdfplumber tables (one list of tables per page). Returns name_key -> entry."""
    out: dict[str, NlemEntry] = {}
    section_no, section_title = 0, ""
    for tables in tables_by_page:
        for table in tables:
            for row in table:
                cells = [c or "" for c in row]
                if not cells or _DEVANAGARI.search(" ".join(cells)):
                    continue
                head = cells[0].strip()
                if m := _SECTION.match(head):
                    section_no = int(m.group(1))
                    title = clean(m.group(2))
                    section_title = title
                    continue
                if len(cells) < 3 or not _CODE.match(head):
                    continue
                name = clean(cells[1]).rstrip("*").strip()
                level = re.sub(r"\s+", "", cells[2])
                if not name or not _LEVELS.match(level):
                    continue
                key = name_key(name)
                if key and key not in out:
                    out[key] = NlemEntry(head, name, level, section_no, section_title)
    return out


def load_nlem(path: Path = NLEM_PDF) -> dict[str, NlemEntry]:
    """Parse the NLEM 2022 PDF main list (stops at the alphabetical index)."""
    import pdfplumber

    pages: list[list] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if re.match(r"\s*Alphabetical\s+List", text):  # the index; the contents page only mentions it
                break
            pages.append(page.extract_tables())
    return parse_tables(pages)


def main() -> int:
    if not NLEM_PDF.exists():
        print(f"missing {NLEM_PDF.relative_to(REPO_ROOT).as_posix()} (download from {NLEM_URL})", file=sys.stderr)
        return 1
    entries = load_nlem()
    sections = {(e.section_no, e.therapeutic_class) for e in entries.values()}
    print(f"NLEM 2022: {len(entries)} medicines in {len(sections)} sections")
    for e in list(entries.values())[:5]:
        print(f"  {e.code:<8} {e.name:<35} {e.level:<6} {e.therapeutic_class}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
