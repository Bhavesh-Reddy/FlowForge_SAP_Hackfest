"""S02: NPPA ceiling-price, Para-19 and NLEM parsers on rows copied from the real source PDFs.

tests/fixtures/nppa_sample.json holds pdfplumber cells copied verbatim from NPPA S.O. 1575(E)
dated 25.03.2026 (5 rows), S.O. 3004(E) dated 11.06.2026 (Para 19) and one NLEM 2022 page.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingest import nlem, nppa, nppa_para19

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nppa_sample.json").read_text(encoding="utf-8"))
ORDER = FIXTURE["ceiling_order"]


def formulations() -> dict[int, nppa.Formulation]:
    return {row.sl_no: nppa.to_formulation(row) for row in map(nppa.parse_row, ORDER["rows"])}


def test_order_header_so_number_order_date_and_wef():
    h = nppa.parse_header(ORDER["english_preamble"], ORDER["header_cells"])
    assert (h.so_number, h.order_date, h.effective_from) == ("1575(E)", date(2026, 3, 25), date(2026, 4, 1))
    assert h.citation == "S.O. 1575(E) dated 25.03.2026"


def test_five_real_rows_parse_with_price_excl_gst_and_previous_so():
    rows = [nppa.parse_row(r) for r in ORDER["rows"]]
    assert [r.sl_no for r in rows] == [12, 42, 47, 59, 92]
    amox = rows[1]
    assert (amox.medicine, amox.form_strength, amox.unit) == ("Amoxicillin", "Capsule 500mg", "1 Capsule")
    assert amox.ceiling_price == Decimal("7.54")
    assert (amox.prev_so, amox.prev_date) == ("1489(E)", "27-03-2025")


def test_names_forms_strengths_and_units_are_normalised():
    f = formulations()
    assert (f[42].generic, f[42].dosage_form, f[42].strength, f[42].unit) == ("Amoxicillin", "Capsule", "500 mg", "1 Capsule")
    assert f[12].generic == "Acetylsalicylic Acid"
    assert f[12].dosage_form == "Conventional/Effervescent/Dispersible/ Enteric Coated Tablet"
    assert (f[47].generic, f[47].strength) == ("Amoxicillin (A) + Clavulanic Acid (B)", "500 mg (A) + 100 mg (B)")
    assert (f[59].generic, f[59].dosage_form, f[59].strength) == ("Anti-Tetanus Immunoglobulin", "As Licensed", "(250 IU)")
    assert (f[92].generic, f[92].strength, f[92].unit) == ("BCG Vaccine", None, "Each Dose (0.10 mL)")


@pytest.mark.parametrize("text, expected", [
    ("Tab. 50mg", ("Tablet", "50 mg")),
    ("Tablets 150 mg", ("Tablet", "150 mg")),
    ("Cap 250 MG", ("Capsule", "250 mg")),
    ("Inj. 1gm", ("Injection", "1 g")),
    ("Powder for injection 500 mg", ("Powder for Injection", "500 mg")),
    ("Respirator solution (Solution for use in nebulizer 5 mg/ml)",
     ("Respirator Solution (Solution for use in Nebulizer)", "5 mg/mL")),
])
def test_dosage_form_abbreviations_and_case(text, expected):
    assert nppa.split_form_strength(text) == expected


def test_form_id_is_stable_and_distinguishes_strengths():
    f = formulations()
    again = nppa.to_formulation(nppa.parse_row(ORDER["rows"][1]))
    assert again.form_id == f[42].form_id == "AMOXICILLIN-0D6327FE"
    assert nppa.make_form_id("Amoxicillin", "Capsule", "250 mg", "1 Capsule") != f[42].form_id
    assert len({x.form_id for x in f.values()}) == 5 and all(len(x.form_id) <= 40 for x in f.values())


def test_header_and_hindi_rows_are_skipped_and_bad_prices_rejected():
    assert nppa.parse_row(ORDER["header_cells"]) is None
    assert nppa.parse_row(["1", "एमोक्सिसिलिन", "कैप्सूल", "1", "7.54", "1489(ऄ)", "27-03-2025"]) is None
    with pytest.raises(ValueError, match="not a number"):
        nppa.parse_row(["1", "X", "Tablet 1 mg", "1 Tablet", "N.A.", "1489(E)", "27-03-2025"])


def test_para19_rows_cite_the_wpi_order_row():
    rows = nppa_para19.parse_table(FIXTURE["para19_table"])
    assert [(r.medicine, r.new_price, r.ref_so, r.ref_sl) for r in rows] == [
        ("Carboplatin", Decimal("90.74"), "1575(E)", 141), ("Cisplatin", Decimal("10.89"), "1575(E)", 182)]
    assert nppa_para19._pct(Decimal("90.74"), Decimal("60.49")) == "50.01"


def test_nlem_level_and_therapeutic_class():
    entries = nlem.parse_tables(FIXTURE["nlem_tables"])
    atropine = entries["atropine"]
    assert (atropine.level, atropine.section_no) == ("P,S,T", 4)
    assert atropine.therapeutic_class.startswith("Antidotes and Other Substances")
    assert entries["desferrioxamine"].level == "S,T"
    # 'Lithium Carbonate' in NPPA matches NLEM 'Lithium'; combinations never match a single molecule
    table = {"lithium": nlem.NlemEntry("23.2.2.1", "Lithium", "S,T", 23, "Psychiatric")}
    assert nppa.nlem_match("Lithium Carbonate", table).name == "Lithium"
    assert nppa.nlem_match("Lithium (A) + Something (B)", table) is None


def test_targets_are_never_invented():
    f = formulations()
    rows = [{"FORM_ID": x.form_id, "GENERIC": x.generic, "DOSAGE_FORM": x.dosage_form, "STRENGTH": x.strength or "",
             "UNIT": x.unit, "NLEM_LEVEL": "", "THERAPEUTIC_CLASS": ""} for x in f.values()]
    prices = [{"FORM_ID": x.form_id, "EFFECTIVE_FROM": "2026-04-01", "SO_NUMBER": "S.O. 1575(E)",
               "CEILING_PRICE": "1"} for x in f.values()]
    with pytest.raises(ValueError, match="matched nothing"):
        nppa.select_targets(rows, prices, [])
