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


def load_fixture_db() -> Db:
    """In-memory SQLite with the SYNTH test fixtures exposed under plan §6 names (tests and --fixtures CLIs)."""
    from agents.ctx import Settings, get_db
    from tests.conftest import FIXTURES, load_fixtures

    db = get_db(Settings(db_backend="sqlite"), sqlite_path=":memory:")
    load_fixtures(db)
    sql = "\n".join(l for l in (FIXTURES / "ref_views.sql").read_text(encoding="utf-8").splitlines()
                    if not l.lstrip().startswith("--"))
    for stmt in filter(str.strip, sql.split(";")):
        db.execute(stmt)
    return db
