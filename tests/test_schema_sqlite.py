"""S01: schema and views create on SQLite, fixtures load, views compute what plan §6 says."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.ctx import Settings, get_db
from ingest import load_hana as lh

FIXTURES = Path(__file__).parent / "fixtures"

SECTION6_TABLES = {
    "FF_REF_FORMULATION", "FF_REF_CEILING_PRICE", "FF_REF_API", "FF_REF_FORM_API",
    "FF_REF_API_COST_MONTHLY", "FF_REF_API_ORIGIN", "FF_REF_PRODUCER", "FF_REF_MANUFACTURER",
    "FF_REF_WPI", "FF_REF_NSQ_ALERT",
    "FF_MM_MARA", "FF_MM_LFA1", "FF_MM_T001W", "FF_MM_MCHB", "FF_MM_MSEG", "FF_MM_EKKO", "FF_MM_EKPO",
    "FF_MM_EBAN", "FF_MM_COLDCHAIN",
    "FF_AG_RUN", "FF_AG_SIGNAL", "FF_AG_DEPENDENCY", "FF_AG_FORECAST", "FF_AG_SCENARIO",
    "FF_AG_RECOMMENDATION", "FF_AG_CHECK", "FF_AG_APPROVAL", "FF_AG_AUDIT_LOG",
    "FF_G_V", "FF_G_E",
}
VIEWS = {"FF_V_WATCHLIST", "FF_V_DAYS_OF_COVER", "FF_V_SUPPLIER_OTD", "FF_V_RECALL_TRACE"}
PROV = {"SOURCE": "test", "IS_PROXY": "SYNTH"}


@pytest.fixture
def db(tmp_path):
    d = get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "s01.sqlite")
    lh.create_objects(d)
    yield d
    d.close()


def put(db, table: str, rows: list[dict]) -> None:
    t = lh.schema_tables()[table]
    lh.upsert_rows(db, t, [{**row, **(PROV if t.has_provenance else {})} for row in rows])


def as_of(db, day: str) -> None:
    put(db, "FF_CFG_PARAM", [{"NAME": "AS_OF_DATE", "DATE_VALUE": day}])


# ---------------------------------------------------------------- schema


def test_generated_sqlite_files_are_in_sync():
    for path, text in lh.render_sqlite_files().items():
        assert path.read_text(encoding="utf-8") == text, f"{path.name} is stale: run --gen-sqlite"


def test_schema_has_every_section6_table_with_provenance():
    tables = lh.schema_tables()
    assert SECTION6_TABLES <= set(tables)
    assert all(name.startswith("FF_") for name in tables)
    for name, t in tables.items():
        if name.startswith(("FF_REF_", "FF_MM_")):
            assert t.has_provenance, name
    audit = tables["FF_AG_AUDIT_LOG"].columns
    assert {"PREV_HASH", "ROW_HASH"} <= set(audit)


def test_schema_and_views_create_on_sqlite(db):
    objects = lh.ff_objects(db)
    assert {n for n, k in objects.items() if k == "table"} == set(lh.schema_tables())
    assert VIEWS <= {n for n, k in objects.items() if k == "view"}
    for v in objects:
        db.query(f'SELECT * FROM "{v}" LIMIT 1')


def test_create_is_idempotent_and_reset_only_touches_ff_objects(db):
    assert lh.create_objects(db)[0] == 0
    db.execute("CREATE TABLE OTHER_TEAM (X INTEGER)")
    dropped = lh.drop_ff_objects(db)
    assert all(n.startswith("FF_") for n in dropped)
    assert lh.ff_objects(db) == {}
    assert not db.query("SELECT name FROM sqlite_master WHERE name = 'OTHER_TEAM'").empty
    assert lh.create_objects(db)[0] == len(lh.schema_tables())


def test_non_portable_view_sql_is_rejected():
    with pytest.raises(ValueError, match="not portable"):
        lh.to_sqlite_views("CREATE VIEW FF_V_X AS SELECT ADD_DAYS(CURRENT_DATE, MONTHS) FROM FF_CFG_PARAM")


# ---------------------------------------------------------------- seed


def test_fixtures_load_and_reload_is_idempotent(db):
    assert FIXTURES.is_dir(), "tests/fixtures/ comes from S00"
    counts, skipped = lh.seed(db, [FIXTURES])
    assert not skipped
    assert counts["FF_REF_FORMULATION"] == 3
    assert counts["FF_REF_API_COST_MONTHLY"] == 24
    assert counts["FF_REF_MANUFACTURER"] == 4
    before = {t: int(db.query(f'SELECT COUNT(*) FROM "{t}"').iat[0, 0]) for t in counts}
    lh.seed(db, [FIXTURES])
    after = {t: int(db.query(f'SELECT COUNT(*) FROM "{t}"').iat[0, 0]) for t in counts}
    assert before == after
    tags = db.query("SELECT DISTINCT IS_PROXY FROM FF_MM_MCHB")["IS_PROXY"].tolist()
    assert tags == ["SYNTH"]
    # quarantined fixture batch is quality-inspection stock, not unrestricted
    q = db.query("SELECT CLABS, CINSM FROM FF_MM_MCHB WHERE CHARG = 'B2604'").iloc[0]
    assert (float(q.CLABS), float(q.CINSM)) == (0.0, 1000.0)


def test_fixture_days_of_cover(db):
    lh.seed(db, [FIXTURES])
    as_of(db, "2026-09-26")
    row = db.query("SELECT * FROM FF_V_DAYS_OF_COVER WHERE MATNR = 'MAT-0001'").iloc[0]
    # expired B2603 and quarantined B2604 excluded: 6000 + 2000 usable; 9120 issued / 90 days
    assert float(row.USABLE_QTY) == 8000
    assert float(row.DAYS_OF_COVER) == round(8000 / (9120 / 90), 1)


def test_table_named_csv_loads_and_bad_rows_are_rejected(db, tmp_path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "FF_REF_API.csv").write_text(
        "API_ID,NAME,HS8,SOURCE,SOURCE_URL,FETCHED_AT,IS_PROXY\n"
        "A9,Test API,29411030,TradeStat,https://example.org,2026-09-26T10:00:00Z,PROXY\n", encoding="utf-8")
    counts, _ = lh.seed(db, [seed_dir])
    assert counts == {"FF_REF_API": 1}

    (seed_dir / "FF_REF_API.csv").write_text(
        "API_ID,NAME,SOURCE,IS_PROXY\nA9,Test API,TradeStat,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="IS_PROXY"):
        lh.seed(db, [seed_dir])

    (seed_dir / "FF_REF_API.csv").write_text(
        "API_ID,NAME,COLOUR,SOURCE,IS_PROXY\nA9,Test API,red,TradeStat,REAL\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown columns"):
        lh.seed(db, [seed_dir])


def test_audit_log_cannot_be_seeded(db):
    with pytest.raises(ValueError, match="append-only"):
        lh.upsert_rows(db, lh.schema_tables()["FF_AG_AUDIT_LOG"], [{"RUN_ID": "R1", "SEQ": 0}])


# ---------------------------------------------------------------- views


@pytest.fixture
def hospital(db):
    """As of 2026-01-31: one material with FEFO-relevant batches, one issued with no stock."""
    as_of(db, "2026-01-31")
    put(db, "FF_REF_FORMULATION", [{"FORM_ID": "F1", "GENERIC": "Amoxicillin"}])
    put(db, "FF_MM_MARA", [{"MATNR": "M1", "FORM_ID": "F1"}, {"MATNR": "M2"}])
    put(db, "FF_MM_T001W", [{"WERKS": "W1", "NAME1": "Central"}, {"WERKS": "W2", "NAME1": "Oncology"}])
    put(db, "FF_MM_MCHB", [
        {"MATNR": "M1", "WERKS": "W1", "LGORT": "CENT", "CHARG": "B0", "CLABS": 30, "VFDAT": "2026-01-01"},
        {"MATNR": "M1", "WERKS": "W1", "LGORT": "CENT", "CHARG": "B1", "CLABS": 100, "VFDAT": "2026-02-20",
         "MANUFACTURER_ID": "M9"},
        {"MATNR": "M1", "WERKS": "W1", "LGORT": "CENT", "CHARG": "B2", "CLABS": 50, "VFDAT": "2026-08-19"},
        {"MATNR": "M1", "WERKS": "W1", "LGORT": "QUAR", "CHARG": "B3", "CLABS": 0, "CINSM": 70,
         "VFDAT": "2027-01-01"},
        {"MATNR": "M3", "WERKS": "W2", "LGORT": "CENT", "CHARG": "B1", "CLABS": 5, "VFDAT": "2027-01-01",
         "MANUFACTURER_ID": "M8"},
    ])
    mseg = [
        ("D1", "261", "M1", 200, "2026-01-21", None, None),
        ("D2", "262", "M1", 20, "2026-01-26", None, None),
        ("D3", "261", "M1", 999, "2025-10-01", None, None),   # outside the 90-day window
        ("D4", "101", "M1", 500, "2026-01-09", "PO1", 10),    # GR, not an issue
        ("D5", "101", "M1", 300, "2026-01-15", "PO1", 20),
        ("D6", "201", "M2", 90, "2026-01-30", None, None),
    ]
    put(db, "FF_MM_MSEG", [{"MBLNR": m, "MJAHR": 2026, "ZEILE": 1, "BWART": b, "MATNR": mat, "WERKS": "W1",
                            "MENGE": q, "BUDAT": d, "EBELN": po, "EBELP": item}
                           for m, b, mat, q, d, po, item in mseg])
    return db


def test_days_of_cover_uses_fefo_usable_unrestricted_stock(hospital):
    doc = hospital.query("SELECT * FROM FF_V_DAYS_OF_COVER ORDER BY MATNR").set_index("MATNR")
    m1 = doc.loc["M1"]
    # net issue (200 - 20) / 90 = 2/day. B0 expired, B3 quarantined.
    # B1: 100 on hand but only 2 * 20 days = 40 can be used before expiry. B2: min(50, 2*200 - 100) = 50.
    assert float(m1.AVG_DAILY_ISSUE_90D) == pytest.approx(2.0)
    assert float(m1.UNEXPIRED_QTY) == 150
    assert float(m1.USABLE_QTY) == 90
    assert float(m1.DAYS_OF_COVER) == 45.0
    assert m1.FORM_ID == "F1"
    # issued but nothing in stock -> zero cover, not missing
    assert float(doc.loc["M2"].DAYS_OF_COVER) == 0.0


def test_supplier_otd(hospital):
    put(hospital, "FF_MM_LFA1", [{"LIFNR": "V1", "NAME1": "Vendor One"}])
    put(hospital, "FF_MM_EKKO", [{"EBELN": "PO1", "LIFNR": "V1", "BEDAT": "2026-01-01"}])
    put(hospital, "FF_MM_EKPO", [
        {"EBELN": "PO1", "EBELP": 10, "MATNR": "M1", "WERKS": "W1", "MENGE": 500, "EINDT": "2026-01-10"},
        {"EBELN": "PO1", "EBELP": 20, "MATNR": "M1", "WERKS": "W1", "MENGE": 300, "EINDT": "2026-01-10"},
        {"EBELN": "PO1", "EBELP": 30, "MATNR": "M1", "WERKS": "W1", "MENGE": 100, "EINDT": "2026-01-20"},
    ])
    r = hospital.query("SELECT * FROM FF_V_SUPPLIER_OTD").iloc[0]
    assert (r.LIFNR, int(r.PO_LINES), int(r.DELIVERED_LINES), int(r.ON_TIME_LINES)) == ("V1", 3, 2, 1)
    assert float(r.OTD_PCT) == 50.0
    assert float(r.AVG_DELAY_DAYS_LATE) == 5.0


def test_recall_trace_matches_batch_and_respects_manufacturer(hospital):
    put(hospital, "FF_REF_NSQ_ALERT", [{"ALERT_ID": "N1", "MONTH": "2026-01-01", "DRUG": "Amoxicillin 500",
                                        "BATCH": " b1 ", "MANUFACTURER_ID": "M9", "FORM_ID": "F1"}])
    rows = hospital.query("SELECT * FROM FF_V_RECALL_TRACE")
    assert rows[["MATNR", "WERKS", "CHARG", "PLANT_NAME", "MATCH_LEVEL"]].values.tolist() == [
        ["M1", "W1", "B1", "Central", "BATCH+MFR+FORM"]]


def test_watchlist_current_ceiling_producers_and_latest_run(hospital):
    put(hospital, "FF_REF_CEILING_PRICE", [
        {"FORM_ID": "F1", "EFFECTIVE_FROM": "2025-01-01", "SO_NUMBER": "SO-1", "CEILING_PRICE": "2.1800"},
        {"FORM_ID": "F1", "EFFECTIVE_FROM": "2026-06-01", "SO_NUMBER": "SO-2", "CEILING_PRICE": "2.5000"},
    ])
    put(hospital, "FF_REF_PRODUCER", [{"FORM_ID": "F1", "MANUFACTURER_ID": m} for m in ("M8", "M9")])
    put(hospital, "FF_AG_RUN", [
        {"RUN_ID": "R1", "STARTED_AT": "2026-01-01T00:00:00", "TRIGGER_TYPE": "NIGHTLY", "STATUS": "DONE"},
        {"RUN_ID": "R2", "STARTED_AT": "2026-01-20T00:00:00", "TRIGGER_TYPE": "NIGHTLY", "STATUS": "DONE"},
    ])
    put(hospital, "FF_AG_FORECAST", [
        {"RUN_ID": r, "FORM_ID": "F1", "EXIT_RISK_BAND": band, "CAUSE_CODE": "HEADROOM_ERODING",
         "CREATED_AT": "2026-01-20T00:00:00"} for r, band in (("R1", "GREEN"), ("R2", "RED"))])
    put(hospital, "FF_AG_SIGNAL", [{"RUN_ID": "R2", "FORM_ID": "F1", "BAND": "AMBER", "HEADROOM_PCT": 4.5,
                                    "CREATED_AT": "2026-01-20T00:00:00"}])
    w = hospital.query("SELECT * FROM FF_V_WATCHLIST").iloc[0]
    assert float(w.CEILING_PRICE) == 2.18 and w.CEILING_SO_NUMBER == "SO-1"
    assert int(w.PRODUCER_COUNT_MIN) == 2
    assert float(w.MIN_DAYS_OF_COVER) == 45.0
    assert (w.RUN_ID, w.EXIT_RISK_BAND, w.MARGIN_BAND) == ("R2", "RED", "AMBER")
    assert w.IS_PROXY == "SYNTH"
