"""Small helpers shared by agents (A2 onward): ctx resolution, optional-table queries, tag handling.

A1 still carries its own copies; fold them in here in a later cleanup.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

from agents.contracts import DataTag, RunContext
from agents.ctx import Db
from agents.rules import Rules, load_rules

_TAG_ORDER = {DataTag.REAL: 0, DataTag.PROXY: 1, DataTag.SYNTH: 2}
_MISSING_TABLE = ("no such table", "invalid table name", "259")


@dataclass
class AgentCtx:
    db: Db
    rules: Rules
    run: RunContext


def resolve_ctx(ctx: Any, prefix: str) -> AgentCtx:
    """Accept a Db, or any object with .db and optional .rules / .run (RunContext)."""
    db = ctx if isinstance(ctx, Db) else ctx.db
    rules = getattr(ctx, "rules", None) or load_rules()
    run = getattr(ctx, "run", None) or RunContext(run_id=f"{prefix}-{uuid.uuid4().hex[:12]}")
    return AgentCtx(db, rules, run)


def try_query(db: Db, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame | None:
    """Run a query; return None if the table/view doesn't exist (optional inputs)."""
    try:
        return db.query(sql, params)
    except Exception as exc:  # sqlite3.OperationalError / hdbcli ProgrammingError
        if any(m in str(exc).lower() for m in _MISSING_TABLE):
            return None
        raise


def to_tag(raw: Any) -> DataTag:
    """Normalise an IS_PROXY / tag cell. Unknown or empty counts as SYNTH (least trusted)."""
    s = str(raw).strip().upper() if raw is not None else ""
    if s in ("REAL", "N", "0", "FALSE"):
        return DataTag.REAL
    if s in ("PROXY", "Y", "1", "TRUE"):
        return DataTag.PROXY
    return DataTag.SYNTH


def worst_tag(values: Sequence[Any]) -> DataTag:
    tags = [to_tag(v) for v in values]
    return max(tags, key=_TAG_ORDER.__getitem__) if tags else DataTag.SYNTH


def placeholders(ids: Sequence[Any]) -> str:
    return ", ".join("?" for _ in ids)


def load_fixture_db(as_of: str | None = None) -> Db:
    """In-memory SQLite with the S01 schema + views, seeded from the SYNTH test fixtures (tests and --fixtures CLIs).

    The fixture CSVs carry no ceiling effective date (the S01 adapter uses FETCHED_AT), so it is set before the
    first cost month; otherwise every earlier month would correctly have no ceiling and A1 could not trend.
    """
    from agents.ctx import Settings, get_db
    from ingest import load_hana as lh
    from tests.conftest import FIXTURES

    db = get_db(Settings(db_backend="sqlite"), sqlite_path=":memory:")
    lh.create_objects(db)
    lh.seed(db, [FIXTURES])
    db.execute("UPDATE FF_REF_CEILING_PRICE SET EFFECTIVE_FROM = ? WHERE SOURCE = ?", (FIXTURE_CEILING_FROM, "FIXTURE_S00"))
    if as_of:
        db.execute("INSERT INTO FF_CFG_PARAM (NAME, DATE_VALUE) VALUES ('AS_OF_DATE', ?)", (as_of,))
    return db


FIXTURE_CEILING_FROM = "2025-04-01"


def table_columns(db: Db, table: str) -> set[str] | None:
    """Upper-case column names of a table/view, or None if it doesn't exist."""
    df = try_query(db, f"SELECT * FROM {table} WHERE 1 = 0")
    return None if df is None else {str(c).upper() for c in df.columns}
