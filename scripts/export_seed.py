"""Dump small FF_ tables to data/seed/<TABLE>.csv so the shared HANA can be rebuilt (plan §7, §11).

    python scripts/export_seed.py --backend hana            # nightly backup of the shared instance
    python scripts/export_seed.py --backend sqlite --out /tmp/x

Restore: python -m ingest.load_hana --backend hana --reset --seed
Skipped: views, FF_TMP_* tables, FF_AG_AUDIT_LOG (append-only; A6 owns it and it cannot be seeded),
tables above --max-rows, and test-fixture rows (SOURCE starting with 'FIXTURE'), so a backup never
mixes test data into the committed seeds. Never prints the HANA host, user or password.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.ctx import REPO_ROOT, SQLITE_PATH, Db, get_db, load_settings  # noqa: E402
from ingest.load_hana import APPEND_ONLY, ff_objects, schema_tables, scrub  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "data" / "seed"


def _cell(v: object) -> object:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, Decimal)):
        return str(v)
    try:  # pandas NaN / NaT
        if v != v:
            return ""
    except Exception:
        pass
    return v


def export(db: Db, out: Path, max_rows: int) -> dict[str, int | str]:
    """Write one CSV per exportable FF_ table (columns in schema order). Returns rows or skip reason per table."""
    tables = schema_tables()
    present = {n for n, k in ff_objects(db).items() if k == "table"}
    result: dict[str, int | str] = {}
    out.mkdir(parents=True, exist_ok=True)
    for name in sorted(present):
        if name not in tables or name.startswith("FF_TMP_"):
            continue
        if name in APPEND_ONLY:
            result[name] = "skipped (append-only)"
            continue
        n = int(db.query(f'SELECT COUNT(*) FROM "{name}"').iat[0, 0])
        if n == 0:
            result[name] = "skipped (empty)"
            continue
        if n > max_rows:
            result[name] = f"skipped ({n} rows > --max-rows {max_rows})"
            continue
        cols = list(tables[name].columns)
        where = " WHERE SOURCE IS NULL OR SOURCE NOT LIKE 'FIXTURE%'" if "SOURCE" in cols else ""
        order = ", ".join(f'"{c}"' for c in tables[name].pk)
        df = db.query(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{name}"{where} ORDER BY {order}')
        with (out / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(cols)
            w.writerows([_cell(v) for v in row] for row in df.itertuples(index=False))
        result[name] = len(df)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=("hana", "sqlite"), help="default: DB_BACKEND from env")
    ap.add_argument("--sqlite-path", default=str(SQLITE_PATH))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--max-rows", type=int, default=50000)
    args = ap.parse_args(argv)
    try:
        settings = load_settings()
        if args.backend:
            settings = dataclasses.replace(settings, db_backend=args.backend)
        with get_db(settings, sqlite_path=args.sqlite_path) as db:
            result = export(db, Path(args.out), args.max_rows)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {scrub(exc)}", file=sys.stderr)
        return 1
    for name, r in result.items():
        print(f"  {name:<28} {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
