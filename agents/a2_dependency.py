"""A2 Dependency Mapper: if one producer exits, who is left, and do they share one upstream
point of failure? (plan §3 A2, §4.2)

Both engines extract the same formulation neighbourhood from FF_G_V / FF_G_E
(built by ingest/build_graph.py) and hand it to one metric function, so they agree by construction:
  - "hana":     openCypher over the HANA Graph workspace FF_SUPPLY_GRAPH (used only if it exists and is valid)
  - "networkx": NetworkX over the same tables (SQLite, or HANA without Graph)

    conc = w1·(1/n_producers_min) + w2·HHI_producers + w3·top_origin_share, clamped to [0, 1]

n_producers_min is a lower bound ("at least N producers"). Writes FF_AG_DEPENDENCY.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import networkx as nx
import pandas as pd

from agents.common import AgentCtx, placeholders, resolve_ctx, try_query, worst_tag
from agents.contracts import DataQuality, DataTag, DependencyProfile
from agents.ctx import Db
from agents.rules import Rules

AGENT = "A2"
WORKSPACE = "FF_SUPPLY_GRAPH"
EDGE_COLS = ["SRC", "DST", "REL", "WEIGHT", "IS_PROXY"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\- ]+$")

# Neighbourhood of formulation f, as four fixed patterns (HANA openCypher; NetworkX mirrors them).
_RET = "RETURN {s}.ID AS SRC, {d}.ID AS DST, e.REL AS REL, e.WEIGHT AS WEIGHT, e.IS_PROXY AS IS_PROXY"
CYPHER = {
    "in_f": "MATCH (s)-[e]->(f) WHERE f.ID = '{fid}' " + _RET.format(s="s", d="f"),
    "in_api": "MATCH (s)-[e]->(a)-[e2]->(f) WHERE f.ID = '{fid}' AND e2.REL = 'INGREDIENT_OF' "
              + _RET.format(s="s", d="a"),
    "out_f": "MATCH (f)-[e]->(m) WHERE f.ID = '{fid}' " + _RET.format(s="f", d="m"),
    "out_mat": "MATCH (f)-[e1]->(m)-[e]->(w) WHERE f.ID = '{fid}' AND e1.REL = 'STOCKED_AS' "
               + _RET.format(s="m", d="w"),
}
CYPHER_SQL = "SELECT SRC, DST, REL, WEIGHT, IS_PROXY FROM OPENCYPHER_TABLE(GRAPH WORKSPACE " + WORKSPACE + " QUERY '{q}')"

DEPENDENCY_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS FF_AG_DEPENDENCY (
  RUN_ID TEXT NOT NULL, FORM_ID TEXT NOT NULL, N_PRODUCERS_MIN INTEGER, HHI REAL,
  TOP_ORIGIN_COUNTRY TEXT, TOP_ORIGIN_SHARE REAL, CONCENTRATION REAL,
  AFFECTED_MATERIALS_JSON TEXT, AFFECTED_WARDS_JSON TEXT, CONFIDENCE REAL, TAGS_JSON TEXT,
  GRAPH_ENGINE TEXT, CREATED_AT TEXT, PRIMARY KEY (RUN_ID, FORM_ID)
)"""
DEPENDENCY_COLS = (
    "RUN_ID", "FORM_ID", "N_PRODUCERS_MIN", "HHI", "TOP_ORIGIN_COUNTRY", "TOP_ORIGIN_SHARE", "CONCENTRATION",
    "AFFECTED_MATERIALS_JSON", "AFFECTED_WARDS_JSON", "CONFIDENCE", "TAGS_JSON", "GRAPH_ENGINE", "CREATED_AT",
)


def fid_vertex(form_id: str) -> str:
    return f"FORMULATION:{form_id}"


def _key(vertex_id: str) -> str:
    return vertex_id.split(":", 1)[1]


# ---------------------------------------------------------------- graph access


def load_edges(db: Db) -> pd.DataFrame:
    """All edges from FF_G_E; builds them in memory from the source tables if FF_G_E is absent, empty,
    or lacks WEIGHT / IS_PROXY (the S01 db/schema.sql shape)."""
    try:
        edges = try_query(db, "SELECT SRC, DST, REL, WEIGHT, IS_PROXY FROM FF_G_E")
    except Exception as exc:  # sqlite "no such column" / HANA "invalid column name"
        if "column" not in str(exc).lower():
            raise
        edges = None
    if edges is None or edges.empty:
        from ingest.build_graph import build_edges

        edges = build_edges(db)[1][EDGE_COLS]
    return edges


def load_graph(db: Db) -> nx.DiGraph:
    g = nx.DiGraph()
    for r in load_edges(db).itertuples(index=False):
        g.add_edge(r.SRC, r.DST, REL=r.REL, WEIGHT=r.WEIGHT, IS_PROXY=r.IS_PROXY)
    return g


def hana_graph_available(db: Db) -> bool:
    """True if the HANA Graph workspace exists and is valid (Day-1 check, verified at runtime)."""
    if db.backend != "hana":
        return False
    try:
        df = db.query(
            "SELECT COUNT(*) AS N FROM SYS.GRAPH_WORKSPACES WHERE SCHEMA_NAME = CURRENT_SCHEMA "
            "AND WORKSPACE_NAME = ? AND IS_VALID = 'TRUE'", (WORKSPACE,))
        return int(df.iloc[0, 0]) > 0
    except Exception:
        return False


def _nx_edge(g: nx.DiGraph, u: str, v: str) -> tuple:
    d = g.edges[u, v]
    return (u, v, d["REL"], d["WEIGHT"], d["IS_PROXY"])


def neighbourhood_nx(g: nx.DiGraph, form_id: str) -> pd.DataFrame:
    f = fid_vertex(form_id)
    rows: list[tuple] = []
    if f in g:
        in_f = [_nx_edge(g, u, f) for u in g.predecessors(f)]
        apis = [e[0] for e in in_f if e[2] == "INGREDIENT_OF"]
        in_api = [_nx_edge(g, u, a) for a in apis for u in g.predecessors(a)]
        out_f = [_nx_edge(g, f, m) for m in g.successors(f)]
        mats = [e[1] for e in out_f if e[2] == "STOCKED_AS"]
        out_mat = [_nx_edge(g, m, w) for m in mats for w in g.successors(m)]
        rows = in_f + in_api + out_f + out_mat
    return _norm(pd.DataFrame(rows, columns=EDGE_COLS))


def neighbourhood_hana(db: Db, form_id: str) -> pd.DataFrame:
    f = fid_vertex(form_id)
    if not _SAFE_ID.match(f):  # openCypher text cannot take bind parameters
        raise ValueError(f"unsafe formulation id {form_id!r}")
    parts = [db.query(CYPHER_SQL.format(q=CYPHER[k].format(fid=f).replace("'", "''"))) for k in CYPHER]
    return _norm(pd.concat([p[EDGE_COLS] for p in parts if len(p)] or [pd.DataFrame(columns=EDGE_COLS)]))


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop_duplicates(["SRC", "DST", "REL"]).sort_values(["REL", "SRC", "DST"]).reset_index(drop=True)
    df["WEIGHT"] = pd.to_numeric(df["WEIGHT"], errors="coerce")
    return df


# ---------------------------------------------------------------- metrics (§4.2)


def hhi(shares: Sequence[float | None]) -> float:
    """Herfindahl index on fractional shares. Missing/zero shares -> equal split."""
    n = len(shares)
    if n == 0:
        return 1.0  # no known producer: treat as fully concentrated
    vals = [s for s in shares if s is not None and not pd.isna(s) and s > 0]
    if len(vals) != n:
        return 1.0 / n
    total = sum(vals)
    return sum((s / total) ** 2 for s in vals)


def concentration(n: int, h: float, top_share: float, rules: Rules) -> float:
    w = lambda k: rules.value("weights", f"concentration.{k}")  # noqa: E731
    inv = 1.0 / n if n > 0 else 1.0
    return min(1.0, max(0.0, w("w_inv_producers") * inv + w("w_hhi") * h + w("w_top_origin_share") * top_share))


def profile_from_edges(form_id: str, edges: pd.DataFrame, run_id: str, rules: Rules) -> DependencyProfile:
    by = {rel: grp for rel, grp in edges.groupby("REL")}
    empty = pd.DataFrame(columns=EDGE_COLS)
    prod, origin = by.get("PRODUCES", empty), by.get("ORIGIN_OF", empty)
    stocked, issued = by.get("STOCKED_AS", empty), by.get("ISSUED_TO", empty)

    n = int(prod["SRC"].nunique())
    h = hhi(prod["WEIGHT"].tolist())
    top_country, top_share = None, 0.0
    if len(origin):
        o = origin.assign(W=origin["WEIGHT"].fillna(0.0)).sort_values(["W", "SRC"], ascending=[False, True])
        top_country, top_share = _key(o.iloc[0]["SRC"]), float(o.iloc[0]["W"])

    tags: dict[str, DataTag] = {}
    if len(prod):
        tags["producers"] = worst_tag(prod["IS_PROXY"].tolist())
    if len(origin):
        tags["api_origin"] = worst_tag(origin["IS_PROXY"].tolist())
    conf = sum(1 for k in ("producers", "api_origin") if tags.get(k) is DataTag.REAL) / 2

    return DependencyProfile(
        run_id=run_id,
        formulation_id=form_id,
        n_producers_min=n,
        hhi=round(h, 6),
        top_origin_country=top_country,
        top_origin_share=round(top_share, 6),
        concentration=round(concentration(n, h, top_share, rules), 6),
        affected_materials=sorted(_key(d) for d in stocked["DST"]),
        affected_wards=sorted({_key(d) for d in issued["DST"]}),
        data_quality=DataQuality(confidence=conf, tags=tags),
    )


# ---------------------------------------------------------------- correlated-risk helper (for A4)


@dataclass
class SharedUpstream:
    """Upstream nodes two supply options have in common. Truthy if any origin country or KSM is shared."""

    countries: list[str] = field(default_factory=list)
    ksms: list[str] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.countries or self.ksms)


def _resolve(g: nx.DiGraph, node: str) -> str:
    if ":" in node and node in g:
        return node
    hits = [f"{t}:{node}" for t in ("FORMULATION", "MANUFACTURER") if f"{t}:{node}" in g]
    if len(hits) != 1:
        raise KeyError(f"{node!r} is {'ambiguous' if hits else 'not a formulation or manufacturer'} in the graph")
    return hits[0]


def upstream(g: nx.DiGraph, node: str, min_share: float) -> tuple[set[str], set[str], set[str]]:
    """(countries, ksms, apis) upstream of a formulation, or of every formulation a manufacturer produces.

    A manufacturer's own API sourcing isn't in the data, so it inherits its formulations' API origins.
    """
    v = _resolve(g, node)
    forms = [v] if v.startswith("FORMULATION:") else [
        d for d in g.successors(v) if g.edges[v, d]["REL"] == "PRODUCES"]
    apis = {a for f in forms for a in g.predecessors(f) if g.edges[a, f]["REL"] == "INGREDIENT_OF"}
    countries, ksms = set(), set()
    for a in apis:
        for u in g.predecessors(a):
            d = g.edges[u, a]
            if d["REL"] == "ORIGIN_OF" and (d["WEIGHT"] or 0) >= min_share:
                countries.add(_key(u))
            elif d["REL"] == "KSM_OF":
                ksms.add(_key(u))
    return countries, ksms, {_key(a) for a in apis}


def shares_upstream(a: str, b: str, ctx: Any, graph: nx.DiGraph | None = None) -> SharedUpstream:
    """Shared origin countries / KSM nodes between a formulation and another formulation or supplier.

    `a` / `b` are formulation or manufacturer ids (bare, e.g. "F001" / "M02", or "TYPE:id").
    A country counts only if it supplies >= graph.shared_origin_min_share of an API's imports.
    """
    c = resolve_ctx(ctx, "a2")
    g = graph if graph is not None else load_graph(c.db)
    min_share = float(c.rules.value("weights", "graph.shared_origin_min_share"))
    ca, ka, aa = upstream(g, a, min_share)
    cb, kb, ab = upstream(g, b, min_share)
    return SharedUpstream(sorted(ca & cb), sorted(ka & kb), sorted(aa & ab))


# ---------------------------------------------------------------- run


def _write(db: Db, profiles: list[DependencyProfile], engine: str) -> None:
    if db.backend == "sqlite":
        db.execute(DEPENDENCY_DDL_SQLITE)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (p.run_id, p.formulation_id, p.n_producers_min, p.hhi, p.top_origin_country, p.top_origin_share,
         p.concentration, json.dumps(p.affected_materials), json.dumps(p.affected_wards),
         p.data_quality.confidence if p.data_quality else None,
         json.dumps({k: v.value for k, v in (p.data_quality.tags if p.data_quality else {}).items()}), engine, now)
        for p in profiles
    ]
    db.executemany(f"INSERT INTO FF_AG_DEPENDENCY ({', '.join(DEPENDENCY_COLS)}) "
                   f"VALUES ({placeholders(DEPENDENCY_COLS)})", rows)


def pick_engine(db: Db, engine: str = "auto") -> str:
    if engine not in ("auto", "hana", "networkx"):
        raise ValueError(f"engine must be auto, hana or networkx, got {engine!r}")
    if engine == "auto":
        return "hana" if hana_graph_available(db) else "networkx"
    return engine


def run(form_ids: Sequence[str], ctx: Any, engine: str = "auto") -> list[DependencyProfile]:
    """One DependencyProfile per formulation; written to FF_AG_DEPENDENCY unless run.dry_run."""
    c: AgentCtx = resolve_ctx(ctx, "a2")
    ids = [str(f) for f in (form_ids or c.run.formulation_ids)]
    if not ids:
        ids = c.db.query("SELECT FORM_ID FROM FF_REF_FORMULATION")["FORM_ID"].astype(str).tolist()
    eng = pick_engine(c.db, engine)
    if eng == "hana":
        hoods = [neighbourhood_hana(c.db, f) for f in ids]
    else:
        g = load_graph(c.db)
        hoods = [neighbourhood_nx(g, f) for f in ids]
    profiles = [profile_from_edges(f, h, c.run.run_id, c.rules) for f, h in zip(ids, hoods)]
    if not c.run.dry_run and profiles:
        _write(c.db, profiles, eng)
    return profiles


# ---------------------------------------------------------------- CLI


def format_table(profiles: list[DependencyProfile]) -> str:
    hdr = f"{'FORM':<8}{'PRODUCERS':>11}{'HHI':>7}{'ORIGIN':>9}{'CONC':>7}{'CONF':>6}  MATERIALS / WARDS"
    lines = [hdr, "-" * len(hdr)]
    for p in profiles:
        origin = f"{p.top_origin_country or '-'} {p.top_origin_share:.0%}" if p.top_origin_country else "-"
        lines.append(
            f"{p.formulation_id:<8}{'>= ' + str(p.n_producers_min):>11}{p.hhi:>7.3f}{origin:>9}"
            f"{(p.concentration or 0):>7.3f}{(p.data_quality.confidence if p.data_quality else 0):>6.2f}  "
            f"{','.join(p.affected_materials) or '-'} / {','.join(p.affected_wards) or '-'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import uuid

    from agents.common import load_fixture_db
    from agents.contracts import RunContext
    from agents.ctx import get_db
    from agents.rules import load_rules

    p = argparse.ArgumentParser(prog="python -m agents.a2_dependency", description=__doc__.split("\n")[0])
    p.add_argument("--form", action="append", default=[], help="formulation id (repeatable; default: all)")
    p.add_argument("--engine", default="auto", choices=("auto", "hana", "networkx"))
    p.add_argument("--fixtures", action="store_true", help="use the SYNTH test fixtures in memory")
    p.add_argument("--dry-run", action="store_true", help="don't write FF_AG_DEPENDENCY")
    a = p.parse_args(argv)
    db = load_fixture_db() if a.fixtures else get_db()
    run_ctx = RunContext(run_id=f"a2-cli-{uuid.uuid4().hex[:8]}", dry_run=a.dry_run)
    with db:
        eng = pick_engine(db, a.engine)
        profiles = run(a.form, AgentCtx(db, load_rules(), run_ctx), engine=eng)
    print(f"run_id={run_ctx.run_id} engine={eng}  (producer counts are lower bounds)")
    print(format_table(profiles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
