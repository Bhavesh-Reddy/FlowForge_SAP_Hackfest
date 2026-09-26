"""Day-1 checks against the shared SAP HANA Cloud instance (plan §7).

    python scripts/day1_checks.py

Reads HANA_* from the environment (or the gitignored .env) and prints a PASS/FAIL table:
connect; create+drop a table; CREATE GRAPH WORKSPACE; PAL (SYS.AFL_AREAS); FF_ row counts.
Never prints the host, user or password. Temp objects are FF_TMP_D1_<random>_* and are always
dropped. Exit code 1 if any check fails.
"""
from __future__ import annotations

import dataclasses
import logging
import secrets
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.ctx import Db, get_db, load_settings  # noqa: E402
from ingest.load_hana import ff_objects, scrub  # noqa: E402

Result = tuple[str, str, str]

# agents.ctx logs the host at INFO when connecting; keep it quiet here.
logging.getLogger("agents.ctx").setLevel(logging.WARNING)


def _first_line(exc: BaseException) -> str:
    return scrub(f"{type(exc).__name__}: {exc}").splitlines()[0][:140]


def _run(name: str, fn: Callable[[], tuple[bool, str]]) -> Result:
    try:
        ok, detail = fn()
        return name, "PASS" if ok else "FAIL", detail
    except Exception as exc:
        return name, "FAIL", _first_line(exc)


def _drop_quietly(db: Db, *stmts: str) -> None:
    for stmt in stmts:
        try:
            db.execute(stmt)
        except Exception:
            pass


def check_create_drop(db: Db, tag: str) -> tuple[bool, str]:
    t = f"FF_TMP_D1_{tag}_T"
    try:
        db.execute(f'CREATE COLUMN TABLE "{t}" (ID INTEGER PRIMARY KEY, V NVARCHAR(10))')
        db.execute(f'INSERT INTO "{t}" VALUES (?, ?)', (1, "ok"))
        n = int(db.query(f'SELECT COUNT(*) FROM "{t}"').iat[0, 0])
        db.execute(f'DROP TABLE "{t}"')
        return n == 1, "create, insert, select, drop ok"
    finally:
        _drop_quietly(db, f'DROP TABLE "{t}"')


def check_graph(db: Db, tag: str) -> tuple[bool, str]:
    v, e, ws = f"FF_TMP_D1_{tag}_V", f"FF_TMP_D1_{tag}_E", f"FF_TMP_D1_{tag}_GW"
    try:
        db.execute(f'CREATE COLUMN TABLE "{v}" (ID NVARCHAR(10) PRIMARY KEY)')
        db.execute(f'CREATE COLUMN TABLE "{e}" (ID NVARCHAR(10) PRIMARY KEY, '
                   'SRC NVARCHAR(10) NOT NULL, DST NVARCHAR(10) NOT NULL)')
        db.executemany(f'INSERT INTO "{v}" VALUES (?)', [("a",), ("b",)])
        db.execute(f'INSERT INTO "{e}" VALUES (?, ?, ?)', ("e1", "a", "b"))
        db.execute(f'CREATE GRAPH WORKSPACE "{ws}" '
                   f'EDGE TABLE "{e}" SOURCE COLUMN SRC TARGET COLUMN DST KEY COLUMN ID '
                   f'VERTEX TABLE "{v}" KEY COLUMN ID')
        db.execute(f'DROP GRAPH WORKSPACE "{ws}"')
        return True, "CREATE GRAPH WORKSPACE allowed"
    finally:
        _drop_quietly(db, f'DROP GRAPH WORKSPACE "{ws}"', f'DROP TABLE "{e}"', f'DROP TABLE "{v}"')


def check_pal(db: Db, _tag: str) -> tuple[bool, str]:
    areas = db.query("SELECT AREA_NAME FROM SYS.AFL_AREAS WHERE AREA_NAME = 'AFLPAL'")
    if areas.empty:
        return False, "AFLPAL area not installed"
    roles = db.query("SELECT COUNT(*) FROM SYS.EFFECTIVE_ROLES WHERE USER_NAME = CURRENT_USER "
                     "AND ROLE_NAME = 'AFL__SYS_AFL_AFLPAL_EXECUTE'")
    granted = int(roles.iat[0, 0]) > 0
    return True, "AFLPAL present; execute role " + ("granted" if granted else "NOT granted (hana-ml PAL may fail)")


def ff_row_counts(db: Db) -> list[tuple[str, int]]:
    tables = sorted(n for n, k in ff_objects(db).items() if k == "table" and not n.startswith("FF_TMP_"))
    return [(t, int(db.query(f'SELECT COUNT(*) FROM "{t}"').iat[0, 0])) for t in tables]


def main() -> int:
    results: list[Result] = []
    counts: list[tuple[str, int]] = []
    db: Db | None = None
    try:
        db = get_db(dataclasses.replace(load_settings(), db_backend="hana"))
        results.append(("connect", "PASS", "TLS connection ok"))
    except Exception as exc:
        results.append(("connect", "FAIL", _first_line(exc)))

    if db is not None:
        tag = secrets.token_hex(3).upper()
        results.append(_run("create+drop table", lambda: check_create_drop(db, tag)))
        results.append(_run("graph workspace", lambda: check_graph(db, tag)))
        results.append(_run("PAL (SYS.AFL_AREAS)", lambda: check_pal(db, tag)))

        def rowcounts() -> tuple[bool, str]:
            counts.extend(ff_row_counts(db))
            hint = "" if counts else " (run: python -m ingest.load_hana --backend hana --reset --seed)"
            return True, f"{len(counts)} FF_ tables, {sum(n for _, n in counts)} rows{hint}"

        results.append(_run("FF_ row counts", rowcounts))
        db.close()

    w = max(len(r[0]) for r in results)
    print(f"{'check'.ljust(w)}  result  detail")
    print(f"{'-' * w}  ------  {'-' * 40}")
    for name, status, detail in results:
        print(f"{name.ljust(w)}  {status:<6}  {detail}")
    if counts:
        print("\nFF_ tables:")
        for t, n in counts:
            print(f"  {t:<28} {n}")
    return 0 if all(s == "PASS" for _, s, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
