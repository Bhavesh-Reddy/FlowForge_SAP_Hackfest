"""Shared pytest fixtures: offline SQLite DB loaded from tests/fixtures/*.csv (all SYNTH)."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from agents.ctx import Db, Settings, get_db
from agents.rules import Rules, load_rules

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixtures(db: Db) -> list[str]:
    """Create one FF_FX_<NAME> table per CSV (all TEXT columns) and load it. Returns table names."""
    tables = []
    for path in sorted(FIXTURES.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        table = f"FF_FX_{path.stem.upper()}"
        cols = ", ".join(f'"{c}" TEXT' for c in header)
        db.execute(f'DROP TABLE IF EXISTS "{table}"')
        db.execute(f'CREATE TABLE "{table}" ({cols})')
        marks = ", ".join("?" for _ in header)
        db.executemany(f'INSERT INTO "{table}" VALUES ({marks})', body)
        tables.append(table)
    return tables


@pytest.fixture
def fixture_db(tmp_path):
    db = get_db(Settings(db_backend="sqlite"), sqlite_path=tmp_path / "ff_test.sqlite")
    load_fixtures(db)
    yield db
    db.close()


@pytest.fixture(scope="session")
def rules() -> Rules:
    return load_rules()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Tests never call a real LLM, whatever the developer's .env says (S11)."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
