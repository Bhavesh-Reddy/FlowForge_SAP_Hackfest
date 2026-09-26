"""Build the supply graph FF_G_V / FF_G_E from the reference and MM tables (plan §3 A2, §6).

Vertices (ID = "<TYPE>:<key>"): KSM, API, COUNTRY, MANUFACTURER, FORMULATION, MATERIAL, WARD.
Edges point downstream, from supply towards the ward:
    KSM -KSM_OF-> API            COUNTRY -ORIGIN_OF(share)-> API
    API -INGREDIENT_OF-> FORM    MANUFACTURER -PRODUCES(market share)-> FORM
    FORM -STOCKED_AS-> MATERIAL  MATERIAL -ISSUED_TO(qty)-> WARD   (MSEG 201/261, ward = KOSTL)
Edges carry WEIGHT and IS_PROXY so A2's metrics and data quality come from the graph alone.

Idempotent: every build replaces the full contents. On HANA, run db/graph.sql first (`--ddl`).
    python -m ingest.build_graph [--ddl] [--fixtures]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from agents.common import try_query
from agents.ctx import Db

VERTEX_TYPES = ("KSM", "API", "COUNTRY", "MANUFACTURER", "FORMULATION", "MATERIAL", "WARD")
ISSUE_MOVEMENTS = ("201", "261")
GRAPH_SQL = Path(__file__).resolve().parent.parent / "db" / "graph.sql"

SQLITE_DDL = (
    "CREATE TABLE IF NOT EXISTS FF_G_V (ID TEXT PRIMARY KEY, TYPE TEXT NOT NULL, LABEL TEXT)",
    "CREATE TABLE IF NOT EXISTS FF_G_E (ID TEXT PRIMARY KEY, SRC TEXT NOT NULL, DST TEXT NOT NULL, "
    "REL TEXT NOT NULL, WEIGHT REAL, IS_PROXY TEXT)",
)


def vid(vtype: str, key: object) -> str:
    return f"{vtype}:{key}"


def _latest(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Keep rows of the latest MONTH per `by` key."""
    m = pd.to_datetime(df["MONTH"].astype(str)).dt.to_period("M")
    return df[m == m.groupby(df[by]).transform("max")]


def build_edges(db: Db) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read reference + MM tables and return (vertices, edges) DataFrames."""
    V: dict[str, tuple[str, str | None]] = {}
    E: list[tuple[str, str, str, float | None, str | None]] = []

    def v(vtype: str, key: object, label: object = None) -> str:
        i = vid(vtype, key)
        if i not in V or (V[i][1] is None and label is not None):
            V[i] = (vtype, None if label is None or pd.isna(label) else str(label))
        return i

    def e(src: str, dst: str, rel: str, weight: object = None, tag: object = None) -> None:
        w = None if weight is None or pd.isna(weight) else float(weight)
        E.append((src, dst, rel, w, None if tag is None or pd.isna(tag) else str(tag)))

    forms = db.query("SELECT FORM_ID, GENERIC, STRENGTH FROM FF_REF_FORMULATION")
    for r in forms.itertuples():
        v("FORMULATION", r.FORM_ID, f"{r.GENERIC} {r.STRENGTH}")

    apis = db.query("SELECT API_ID, NAME, KSM_ID, IS_PROXY FROM FF_REF_API")
    for r in apis.itertuples():
        a = v("API", r.API_ID, r.NAME)
        if r.KSM_ID is not None and not pd.isna(r.KSM_ID) and str(r.KSM_ID).strip():
            e(v("KSM", r.KSM_ID), a, "KSM_OF", None, r.IS_PROXY)

    origin = db.query("SELECT API_ID, MONTH, COUNTRY, SHARE, IS_PROXY FROM FF_REF_API_ORIGIN")
    if len(origin):
        for r in _latest(origin, "API_ID").itertuples():
            e(v("COUNTRY", r.COUNTRY, r.COUNTRY), v("API", r.API_ID), "ORIGIN_OF", r.SHARE, r.IS_PROXY)

    for r in db.query("SELECT FORM_ID, API_ID FROM FF_REF_FORM_API").itertuples():
        e(v("API", r.API_ID), v("FORMULATION", r.FORM_ID), "INGREDIENT_OF")

    names = try_query(db, "SELECT ID, NAME FROM FF_REF_MANUFACTURER")
    name_of = dict(zip(names["ID"].astype(str), names["NAME"])) if names is not None else {}
    prod = try_query(db, "SELECT FORM_ID, MANUFACTURER_ID, MARKET_SHARE, IS_PROXY FROM FF_REF_PRODUCER")
    if prod is None:  # §6 shape without MARKET_SHARE: A2 then assumes equal shares
        prod = db.query("SELECT FORM_ID, MANUFACTURER_ID, IS_PROXY FROM FF_REF_PRODUCER").assign(MARKET_SHARE=None)
    for r in prod.drop_duplicates(["FORM_ID", "MANUFACTURER_ID"]).itertuples():
        m = v("MANUFACTURER", r.MANUFACTURER_ID, name_of.get(str(r.MANUFACTURER_ID)))
        e(m, v("FORMULATION", r.FORM_ID), "PRODUCES", r.MARKET_SHARE, r.IS_PROXY)

    mara = try_query(db, "SELECT MATNR, MAKTX, FORM_ID FROM FF_MM_MARA")
    if mara is not None:
        for r in mara.dropna(subset=["FORM_ID"]).itertuples():
            e(v("FORMULATION", r.FORM_ID), v("MATERIAL", r.MATNR, r.MAKTX), "STOCKED_AS", None, "SYNTH")
        mseg = try_query(db, "SELECT MATNR, KOSTL, BWART, MENGE FROM FF_MM_MSEG")
        if mseg is not None and len(mseg):
            gi = mseg[mseg["BWART"].astype(str).isin(ISSUE_MOVEMENTS) & mseg["KOSTL"].notna()]
            gi = gi[gi["MATNR"].isin(set(mara["MATNR"]))]
            for (matnr, kostl), qty in gi.groupby(["MATNR", "KOSTL"])["MENGE"].sum().items():
                e(v("MATERIAL", matnr), v("WARD", kostl, kostl), "ISSUED_TO", qty, "SYNTH")

    vdf = pd.DataFrame([(i, t, l) for i, (t, l) in sorted(V.items())], columns=["ID", "TYPE", "LABEL"])
    edf = pd.DataFrame(E, columns=["SRC", "DST", "REL", "WEIGHT", "IS_PROXY"])
    edf.insert(0, "ID", edf["REL"] + "|" + edf["SRC"] + "|" + edf["DST"])
    edf = edf.drop_duplicates("ID").sort_values("ID").reset_index(drop=True)
    return vdf, edf


def run_ddl(db: Db) -> None:
    """Create FF_G_V / FF_G_E (and on HANA the graph workspace). Safe to re-run."""
    if db.backend == "sqlite":
        for stmt in SQLITE_DDL:
            db.execute(stmt)
        return
    sql = "\n".join(l for l in GRAPH_SQL.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("--"))
    for stmt in filter(str.strip, sql.split(";")):
        try:
            db.execute(stmt)
        except Exception as exc:  # DROP of a missing object on first run
            if not stmt.strip().upper().startswith("DROP"):
                raise
            del exc


def build(db: Db) -> tuple[int, int]:
    """Replace FF_G_V / FF_G_E with a fresh build. Returns (n_vertices, n_edges)."""
    if db.backend == "sqlite":
        run_ddl(db)
    vdf, edf = build_edges(db)
    db.execute("DELETE FROM FF_G_E")
    db.execute("DELETE FROM FF_G_V")
    db.executemany("INSERT INTO FF_G_V (ID, TYPE, LABEL) VALUES (?, ?, ?)",
                   vdf.astype(object).where(vdf.notna(), None).itertuples(index=False))
    db.executemany("INSERT INTO FF_G_E (ID, SRC, DST, REL, WEIGHT, IS_PROXY) VALUES (?, ?, ?, ?, ?, ?)",
                   edf.astype(object).where(edf.notna(), None).itertuples(index=False))
    return len(vdf), len(edf)


def main(argv: list[str] | None = None) -> int:
    from agents.common import load_fixture_db
    from agents.ctx import get_db

    p = argparse.ArgumentParser(prog="python -m ingest.build_graph", description="Build FF_G_V / FF_G_E")
    p.add_argument("--ddl", action="store_true", help="(re)create the graph tables and, on HANA, the workspace")
    p.add_argument("--fixtures", action="store_true", help="build from the SYNTH test fixtures in memory")
    a = p.parse_args(argv)
    with (load_fixture_db() if a.fixtures else get_db()) as db:
        if a.ddl:
            run_ddl(db)
        nv, ne = build(db)
        types = db.query("SELECT TYPE, COUNT(*) AS N FROM FF_G_V GROUP BY TYPE ORDER BY TYPE")
    print(f"FF_G_V={nv} FF_G_E={ne}  " + " ".join(f"{t}={n}" for t, n in types.itertuples(index=False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
