from types import SimpleNamespace

import pandas as pd
import pytest

from agents import a2_dependency as a2
from agents.common import load_fixture_db
from agents.contracts import DataTag, RunContext
from ingest.build_graph import build, build_edges


@pytest.fixture
def db():
    d = load_fixture_db()
    yield d
    d.close()


def _ctx(db, rules, **run):
    return SimpleNamespace(db=db, rules=rules, run=RunContext(run_id="t-a2", **run))


def _by_form(profiles):
    return {p.formulation_id: p for p in profiles}


def test_graph_build_types_and_idempotent(db):
    nv, ne = build(db)
    assert (nv, ne) == build(db)  # rebuild replaces, doesn't duplicate
    types = set(db.query("SELECT DISTINCT TYPE FROM FF_G_V")["TYPE"])
    assert types == {"API", "COUNTRY", "MANUFACTURER", "FORMULATION", "MATERIAL", "WARD"}  # no KSM in fixtures
    rels = set(db.query("SELECT DISTINCT REL FROM FF_G_E")["REL"])
    assert rels == {"ORIGIN_OF", "INGREDIENT_OF", "PRODUCES", "STOCKED_AS", "ISSUED_TO"}
    e = db.query("SELECT WEIGHT FROM FF_G_E WHERE SRC = ? AND DST = ?", ("COUNTRY:CN", "API:A01"))
    assert e.iloc[0]["WEIGHT"] == pytest.approx(0.82)


def test_profiles_on_fixtures(db, rules):
    p = _by_form(a2.run([], _ctx(db, rules, dry_run=True), engine="networkx"))
    f1 = p["F001"]
    assert f1.n_producers_min == 3
    assert f1.hhi == pytest.approx(0.55**2 + 0.30**2 + 0.15**2)
    assert (f1.top_origin_country, f1.top_origin_share) == ("CN", pytest.approx(0.82))
    assert f1.concentration == pytest.approx(0.5 / 3 + 0.3 * 0.415 + 0.2 * 0.82, abs=1e-6)
    assert f1.affected_materials == ["MAT-0001"]
    assert f1.affected_wards == ["GEN-MED", "PAEDS", "SURG"]
    assert f1.data_quality.confidence == 0.0 and set(f1.data_quality.tags.values()) == {DataTag.SYNTH}
    assert p["F002"].n_producers_min == 2 and p["F002"].hhi == pytest.approx(0.58)
    assert p["F002"].concentration > f1.concentration > p["F003"].concentration
    assert p["F003"].affected_materials == [] and p["F003"].top_origin_share == pytest.approx(0.61)


def test_equal_shares_when_market_share_missing(db, rules):
    db.execute("UPDATE FF_FX_PRODUCERS SET MARKET_SHARE = NULL WHERE FORMULATION_ID = ?", ("F001",))
    [f1] = a2.run(["F001"], _ctx(db, rules, dry_run=True), engine="networkx")
    assert f1.hhi == pytest.approx(1 / 3)


def test_unknown_formulation(db, rules):
    [p] = a2.run(["NOPE"], _ctx(db, rules, dry_run=True), engine="networkx")
    assert p.n_producers_min == 0 and p.hhi == 1.0 and p.concentration == pytest.approx(0.8)
    assert p.data_quality.confidence == 0.0


def test_confidence_counts_real_inputs(db, rules):
    db.execute("UPDATE FF_FX_APIS SET IS_PROXY = 'REAL'")
    [f1] = a2.run(["F001"], _ctx(db, rules, dry_run=True), engine="networkx")
    assert f1.data_quality.tags["api_origin"] is DataTag.REAL and f1.data_quality.confidence == 0.5


def test_writes_ff_ag_dependency(db, rules):
    a2.run(["F001", "F002"], _ctx(db, rules), engine="networkx")
    rows = db.query("SELECT RUN_ID, FORM_ID, N_PRODUCERS_MIN, GRAPH_ENGINE FROM FF_AG_DEPENDENCY ORDER BY FORM_ID")
    assert rows["FORM_ID"].tolist() == ["F001", "F002"] and set(rows["RUN_ID"]) == {"t-a2"}
    assert rows["N_PRODUCERS_MIN"].tolist() == [3, 2] and set(rows["GRAPH_ENGINE"]) == {"networkx"}


def test_uses_ff_g_e_when_built(db, rules):
    build(db)
    db.execute("DELETE FROM FF_G_E WHERE SRC = ?", ("MANUFACTURER:M03",))  # graph tables are the source of truth
    [f1] = a2.run(["F001"], _ctx(db, rules, dry_run=True), engine="networkx")
    assert f1.n_producers_min == 2


class FakeHanaDb:
    """Stands in for HANA: answers each openCypher pattern with an equivalent SQL join over FF_G_E."""

    SQL = {
        "in_f": "SELECT SRC, DST, REL, WEIGHT, IS_PROXY FROM FF_G_E WHERE DST = ?",
        "in_api": "SELECT e.SRC, e.DST, e.REL, e.WEIGHT, e.IS_PROXY FROM FF_G_E e JOIN FF_G_E e2 ON e2.SRC = e.DST "
                  "WHERE e2.DST = ? AND e2.REL = 'INGREDIENT_OF'",
        "out_f": "SELECT SRC, DST, REL, WEIGHT, IS_PROXY FROM FF_G_E WHERE SRC = ?",
        "out_mat": "SELECT e.SRC, e.DST, e.REL, e.WEIGHT, e.IS_PROXY FROM FF_G_E e JOIN FF_G_E e1 ON e1.DST = e.SRC "
                   "WHERE e1.SRC = ? AND e1.REL = 'STOCKED_AS'",
    }
    backend = "hana"

    def __init__(self, inner):
        self.inner = inner
        self.cypher_calls = 0

    def query(self, sql, params=()):
        if "GRAPH_WORKSPACES" in sql:
            return pd.DataFrame({"N": [1]})
        if "OPENCYPHER_TABLE" in sql:
            self.cypher_calls += 1
            for key, tmpl in a2.CYPHER.items():
                for form in ("F001", "F002", "F003"):
                    if tmpl.format(fid=a2.fid_vertex(form)).replace("'", "''") in sql:
                        return self.inner.query(self.SQL[key], (a2.fid_vertex(form),))
            raise AssertionError(f"unexpected cypher: {sql}")
        return self.inner.query(sql, params)

    def execute(self, *a):
        return self.inner.execute(*a)

    def executemany(self, *a):
        return self.inner.executemany(*a)


def test_hana_and_networkx_paths_identical(db, rules):
    build(db)
    fake = FakeHanaDb(db)
    assert a2.pick_engine(fake) == "hana" and a2.pick_engine(db) == "networkx"
    hana = a2.run(["F001", "F002", "F003"], _ctx(fake, rules, dry_run=True))
    netx = a2.run(["F001", "F002", "F003"], _ctx(db, rules, dry_run=True), engine="networkx")
    assert fake.cypher_calls == 12
    assert [p.model_dump() for p in hana] == [p.model_dump() for p in netx]


def test_hana_path_rejects_unsafe_id(db):
    with pytest.raises(ValueError):
        a2.neighbourhood_hana(FakeHanaDb(db), "F001' OR 1=1 --")


# ---- correlated-risk helper (A4)


def test_two_producers_share_one_api_origin(db, rules):
    # M01 (F001, F002) and M04 (F002, F003): all their APIs come mainly from CN
    s = a2.shares_upstream("M01", "M04", _ctx(db, rules))
    assert s and s.countries == ["CN"] and s.ksms == []
    s2 = a2.shares_upstream("F001", "M02", _ctx(db, rules))  # alternate producer of the same formulation
    assert s2.countries == ["CN"] and "A01" in s2.apis


def test_different_origins_not_shared(db, rules):
    db.execute("UPDATE FF_FX_APIS SET TOP_ORIGIN_COUNTRY = 'IN' WHERE API_ID = ?", ("A02",))
    s = a2.shares_upstream("F001", "F003", _ctx(db, rules))
    assert not s and s.countries == [] and s.apis == []


def test_minor_origin_below_threshold_ignored(db, rules):
    db.execute("UPDATE FF_FX_APIS SET TOP_ORIGIN_SHARE = '0.1' WHERE API_ID = ?", ("A02",))
    assert not a2.shares_upstream("F001", "F003", _ctx(db, rules))


def test_shared_ksm(db, rules):
    db.execute("DROP VIEW FF_REF_API")
    db.execute("CREATE TABLE FF_REF_API (API_ID TEXT, NAME TEXT, HS8 TEXT, KSM_ID TEXT, IS_PROXY TEXT)")
    db.executemany("INSERT INTO FF_REF_API VALUES (?, ?, ?, ?, ?)",
                   [("A01", "Amoxicillin trihydrate", "29411030", "K-6APA", "SYNTH"),
                    ("A02", "Paracetamol", "29242930", "K-6APA", "SYNTH")])
    db.execute("UPDATE FF_FX_APIS SET TOP_ORIGIN_COUNTRY = 'IN' WHERE API_ID = ?", ("A02",))
    s = a2.shares_upstream("F001", "F003", _ctx(db, rules))
    assert s and s.ksms == ["K-6APA"] and s.countries == []
    _, edges = build_edges(db)
    assert (edges["REL"] == "KSM_OF").sum() == 2


def test_unknown_node_raises(db, rules):
    with pytest.raises(KeyError):
        a2.shares_upstream("F001", "ZZZ", _ctx(db, rules))


def test_cli(capsys):
    assert a2.main(["--fixtures", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "engine=networkx" in out and ">= 3" in out
