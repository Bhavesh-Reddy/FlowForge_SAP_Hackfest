"""Rebuild the demo database and prove the §10 demo flow works (plan §10, §11, S13).

    python scripts/demo_reset.py --backend sqlite            # reset + seed + baseline (< 1 min)
    python scripts/demo_reset.py --backend hana              # same for the shared HANA (< 5 min)
    python scripts/demo_reset.py --backend sqlite --check    # ... then run the whole demo flow
    python scripts/demo_reset.py --backend hana --prepare    # finale morning: reset, baseline, save the shocked run

Steps: drop and recreate FF_ objects in our own schema, load data/seed/ (NOT tests/fixtures: fixture rows
would appear on the watchlist), run the baseline A1-A5 for every tracked formulation. With --check, the
§10 flow is driven through the same FastAPI routes the Build Apps / fallback screens call:
  shock +30% -> >= 3 RED -> a detail page with a correlated alternate rejected -> approve with the full
  checklist (level 2 if required) -> PR payload in FF_MM_EBAN -> audit chain ok -> recall trace finds the
  planted NSQ batch in >= 3 locations.
On HANA the agents do not run over the network for the reset: from a laptop every query is a ~300 ms
round trip, a run needs ~2,500 of them, and the shared instance drops such long sessions. The same agents
run on a local SQLite mirror of the identical seed data (same code, same rules, deterministic), and the
FF_AG_* rows (runs, signals, forecasts, scenarios, recommendations, checks, audit chain) are published to
HANA in batched inserts. The HANA --check then drives detail, approval, PR, audit and recall through the API
against HANA, using that published shocked run (so run --prepare first).
LLM_PROVIDER is forced to 'none' and the API Hub master-data check is skipped, so it works offline.
Never prints the HANA host, user or password. Exit code 1 if a step fails or a time budget is exceeded.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LLM_PROVIDER"] = "none"  # before any settings are loaded

from agents import orchestrator as orch  # noqa: E402
from agents.ctx import REPO_ROOT, SQLITE_PATH, Db, get_db, load_settings  # noqa: E402
from ingest import load_hana as lh  # noqa: E402

SEED_DIR = REPO_ROOT / "data" / "seed"
MIRROR_PATH = REPO_ROOT / "data" / "demo_mirror.sqlite"  # gitignored (*.sqlite)
PUBLISH_TABLES = ("FF_AG_RUN", "FF_AG_SIGNAL", "FF_AG_DEPENDENCY", "FF_AG_FORECAST", "FF_AG_SCENARIO",
                  "FF_AG_RECOMMENDATION", "FF_AG_CHECK", "FF_AG_AUDIT_LOG")
BUDGET_S = {"sqlite": 60, "hana": 300}
SHOCK = 1.3
MIN_RED = 3
MIN_RECALL_LOCATIONS = 3
CHECKLIST = {k: True for k in ("cp01_material_master", "cp02_rol_roq_stock", "cp04_supplier_verified",
                                 "price_within_ceiling_gst", "shelf_life_fefo_ok", "cause_reviewed_not_claim")}
Step = tuple[str, bool, str]


def report(steps: list[Step], name: str, passed: bool, detail: str) -> bool:
    """Record a step and print it at once (a long HANA run should show progress)."""
    steps.append((name, bool(passed), detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}: {detail}", flush=True)
    return bool(passed)


def reset_and_seed(db: Db, steps: list[Step]) -> None:
    t = time.perf_counter()
    dropped = lh.drop_ff_objects(db)
    n_tables, n_views = lh.create_objects(db)
    counts, _ = lh.seed(db, [SEED_DIR], fresh=True)
    report(steps, "reset + seed", True, f"dropped {len(dropped)}, created {n_tables} tables / {n_views} views, "
                                        f"{sum(counts.values())} rows in {time.perf_counter() - t:.1f}s")


def agent_run(db: Db, steps: list[Step], shock: float) -> str:
    """A1-A5 for every tracked formulation, written to FF_AG_* (baseline when shock == 1.0)."""
    from api.main import tracked_form_ids

    t = time.perf_counter()
    ids = tracked_form_ids(db)
    r = orch.run(ids, shock, db=db)
    name = "baseline run" if shock == 1.0 else f"shocked run x{shock:g} (saved for the demo)"
    report(steps, name, r.chain.ok, f"{len(ids)} formulations, status {r.status}, {len(r.gate)} at the gate, "
                                    f"chain {'ok' if r.chain.ok else 'BROKEN'} in {time.perf_counter() - t:.1f}s")
    return r.run_id


def publish(src: Db, dst: Db, steps: list[Step]) -> None:
    """Copy the agent-layer rows computed on the mirror into HANA (fresh tables after --reset)."""
    import math

    t = time.perf_counter()
    tables = lh.schema_tables()
    total = 0
    for name in PUBLISH_TABLES:
        cols = list(tables[name].columns)
        df = src.query(f'SELECT {", ".join(cols)} FROM "{name}"')
        rows = []
        for rec in df.itertuples(index=False):
            vals = []
            for col, v in zip(cols, rec):
                typ = tables[name].columns[col]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    vals.append(None)
                else:
                    if typ in ("INTEGER", "BIGINT", "TINYINT", "SMALLINT") and isinstance(v, float):
                        v = int(v)  # pandas turns an integer column with NULLs into floats (1.0)
                    vals.append(lh._convert(v, typ, name, col))
            rows.append(tuple(vals))
        sql = f'INSERT INTO "{name}" ({", ".join(cols)}) VALUES ({", ".join("?" for _ in cols)})'
        for i in range(0, len(rows), 500):
            dst.executemany(sql, rows[i:i + 500])
        total += len(rows)
    report(steps, "publish agent results to HANA", True,
           f"{total} rows into {len(PUBLISH_TABLES)} FF_AG_* tables in {time.perf_counter() - t:.1f}s")


def check_flow(db_factory: Callable[[], Db], settings: Any, live_run: bool = True) -> list[Step]:
    """The §10 demo, through the API (in-process), with a throwaway API key.

    live_run=False (HANA): use the latest saved shocked run instead of POST /run."""
    from fastapi.testclient import TestClient
    from api.main import create_app

    key = secrets.token_hex(8)
    app = create_app(dataclasses.replace(settings, app_api_key=key, llm_provider="none"), db_factory=db_factory)
    c = TestClient(app, headers={"X-API-Key": key})
    steps: list[Step] = []

    def ok(name: str, cond: bool, detail: str) -> bool:
        return report(steps, name, cond, detail)

    # 1. what-if shock (A1 only), then the real shocked run
    shock = c.post("/scenario/shock", json={"multiplier": SHOCK})
    red_whatif = [r["form_id"] for r in shock.json()["rows"] if r["shocked"]["band"] == "RED"] if shock.is_success else []
    ok("shock +30% (what-if)", shock.is_success and len(red_whatif) >= MIN_RED, f"{len(red_whatif)} RED on margin (A1)")
    if live_run:
        run = c.post("/run", json={"shock": SHOCK})
        if not ok("shocked run A1-A5", run.is_success and run.json()["chain_ok"], f"HTTP {run.status_code}"):
            return steps
        body = run.json()
        run_id = body["run_id"]
        red = [s["formulation_id"] for s in body["signals"] if s["band"] == "RED"]
        at_gate = len(body["awaiting_approval"])
    else:
        db = db_factory()
        try:
            runs = db.query("SELECT RUN_ID FROM FF_AG_RUN WHERE TRIGGER_TYPE = 'SHOCK' ORDER BY STARTED_AT DESC")
            run_id = str(runs.iloc[0]["RUN_ID"]) if len(runs) else None
            red = db.query("SELECT FORM_ID FROM FF_AG_SIGNAL WHERE RUN_ID = ? AND BAND = 'RED'",
                           (run_id,))["FORM_ID"].astype(str).tolist() if run_id else []
            at_gate = int(db.query("SELECT COUNT(*) FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ? AND OVERALL IN "
                                   "('READY', 'NEEDS_CHANGES')", (run_id,)).iat[0, 0]) if run_id else 0
        finally:
            db.close()
        if not ok("saved shocked run", run_id is not None, f"{run_id} (run --prepare first)"):
            return steps
    ok(f">= {MIN_RED} RED", len(red) >= MIN_RED, f"{len(red)} RED signals, {at_gate} at the gate")

    # 2. a detail page with a correlated alternate rejected, for a RED formulation that has a PR to approve
    pending = {p["rec_id"]: p for p in c.get("/approvals/pending").json()}
    chosen, rec = None, None
    for form_id in red:
        detail = c.get(f"/molecule/{form_id}").json()
        rejected = [s for s in detail.get("scenarios", []) if s["correlated_risk_flag"] and s["rank"] is None
                    and s["run_id"] == run_id]
        options = [p for p in pending.values() if p["form_id"] == form_id and p["run_id"] == run_id
                   and p["draft_pr"] and p["overall"] == "READY"]
        if rejected and options:
            chosen, rec = (form_id, rejected[0]), sorted(options, key=lambda p: p["scenario"]["rank"] or 99)[0]
            break
    if not ok("detail: correlated alternate rejected", chosen is not None,
              f"{chosen[0]}: {chosen[1]['correlated_risk_reason']}" if chosen else "no RED formulation with one"):
        return steps

    # 3. human gate: full checklist; level 2 by a different person if required
    a = c.post("/approval", json={"rec_id": rec["rec_id"], "decision": "APPROVE", "user": "chief.pharmacist",
                                  "checklist": CHECKLIST, "level": 1})
    status = a.json().get("status") if a.is_success else f"HTTP {a.status_code}: {a.text[:120]}"
    if a.is_success and status == "AWAITING_SECOND_APPROVER":
        a = c.post("/approval", json={"rec_id": rec["rec_id"], "decision": "APPROVE", "user": "director",
                                      "checklist": CHECKLIST, "level": 2})
        status = a.json().get("status") if a.is_success else f"HTTP {a.status_code}: {a.text[:120]}"
    banfn = a.json().get("banfn") if a.is_success else None
    ok("approve with checklist", status == "ACTIONED" and banfn, f"{rec['rec_id']} -> {status}, PR {banfn}")

    # 4. PR payload stored in FF_MM_EBAN
    db = db_factory()
    try:
        eban = db.query("SELECT BANFN, MATNR, MENGE, RUN_ID, SCENARIO_ID FROM FF_MM_EBAN WHERE BANFN = ?", (banfn,))
    finally:
        db.close()
    ok("PR in FF_MM_EBAN", len(eban) == 1 and eban.iloc[0]["SCENARIO_ID"] == rec["rec_id"],
       f"{len(eban)} row(s) for {banfn}")

    # 5. audit chain (A1-A5 hops + gate + action)
    au = c.get(f"/audit/{run_id}").json()
    ok("audit chain ok", au["chain"]["ok"], f"{au['chain']['n_rows']} rows, broken_at {au['chain']['broken_at']}")

    # 6. recall: the planted NSQ batch in >= 3 locations
    db = db_factory()
    try:
        tr = db.query("SELECT ALERT_ID, COUNT(DISTINCT LGORT) AS N FROM FF_V_RECALL_TRACE GROUP BY ALERT_ID "
                      "ORDER BY N DESC")
    finally:
        db.close()
    if ok("recall alert present", len(tr) > 0, f"{len(tr)} alert(s) match hospital stock"):
        rc = c.get(f"/recall/{tr.iloc[0]['ALERT_ID']}").json()
        locs = sorted({m["LGORT"] for m in rc["matches"]})
        ok(f"recall in >= {MIN_RECALL_LOCATIONS} locations", len(locs) >= MIN_RECALL_LOCATIONS,
           f"batch {rc['alert'].get('BATCH')} at {', '.join(locs)}")
    return steps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=("hana", "sqlite"), required=True)
    ap.add_argument("--sqlite-path", default=str(SQLITE_PATH))
    ap.add_argument("--check", action="store_true", help="after the reset, run the whole §10 demo flow")
    ap.add_argument("--prepare", action="store_true",
                    help="after the baseline, save the +30%% shocked run so the demo pages show it (nothing approved)")
    args = ap.parse_args(argv)

    settings = dataclasses.replace(load_settings(), db_backend=args.backend, llm_provider="none")

    def factory() -> Db:
        return get_db(settings, sqlite_path=args.sqlite_path)

    steps: list[Step] = []
    t0 = time.perf_counter()
    print(f"[demo_reset] backend={args.backend}", flush=True)
    try:
        # a fresh connection per phase: one long-lived session was cut by the HANA side on a slow link
        with factory() as db:
            reset_and_seed(db, steps)
        if args.backend == "sqlite":
            with factory() as db:
                agent_run(db, steps, 1.0)
                if args.prepare:
                    agent_run(db, steps, SHOCK)
        else:
            mirror_settings = dataclasses.replace(settings, db_backend="sqlite")
            with get_db(mirror_settings, sqlite_path=MIRROR_PATH) as mirror:
                lh.drop_ff_objects(mirror)
                lh.create_objects(mirror)
                lh.seed(mirror, [SEED_DIR])
                agent_run(mirror, steps, 1.0)
                if args.prepare or args.check:
                    agent_run(mirror, steps, SHOCK)
                with factory() as db:
                    publish(mirror, db, steps)
        elapsed = time.perf_counter() - t0
        report(steps, f"time budget ({args.backend})", elapsed <= BUDGET_S[args.backend],
               f"reset + seed + baseline{' + shocked run' if args.prepare else ''} {elapsed:.1f}s "
               f"of {BUDGET_S[args.backend]}s")
        if args.check:
            steps += check_flow(factory, settings, live_run=args.backend == "sqlite")
            if args.prepare:
                print("  note: --check approved a recommendation; run --prepare again before presenting", flush=True)
    except Exception as exc:  # never leak credentials
        report(steps, "error", False, f"{type(exc).__name__}: {lh.scrub(exc)}"[:200])

    w = max(len(n) for n, _, _ in steps)
    print()
    print(f"{'step'.ljust(w)}  result  detail")
    for name, passed, detail in steps:
        print(f"{name.ljust(w)}  {'PASS' if passed else 'FAIL':<6}  {detail}")
    print(f"total {time.perf_counter() - t0:.1f}s")
    return 0 if all(p for _, p, _ in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
