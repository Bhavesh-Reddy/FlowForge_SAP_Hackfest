-- FF supply graph for HANA Cloud (plan §3 A2, §6). HANA only; SQLite uses ingest/build_graph.py DDL + NetworkX.
-- Idempotent: run via `python -m ingest.build_graph --ddl` (DROP errors on a first run are ignored),
-- then `python -m ingest.build_graph` to (re)load rows. Objects live in our own schema (HANA_SCHEMA).
-- Day-1 check: if CREATE GRAPH WORKSPACE is refused, A2 falls back to NetworkX over the same tables.

DROP GRAPH WORKSPACE FF_SUPPLY_GRAPH;
DROP TABLE FF_G_E;
DROP TABLE FF_G_V;

CREATE COLUMN TABLE FF_G_V (
  ID     NVARCHAR(128) NOT NULL PRIMARY KEY,   -- "<TYPE>:<key>", e.g. API:A01
  TYPE   NVARCHAR(16)  NOT NULL,               -- KSM | API | COUNTRY | MANUFACTURER | FORMULATION | MATERIAL | WARD
  LABEL  NVARCHAR(256)
);

CREATE COLUMN TABLE FF_G_E (
  ID       NVARCHAR(400) NOT NULL PRIMARY KEY, -- "<REL>|<SRC>|<DST>"
  SRC      NVARCHAR(128) NOT NULL,
  DST      NVARCHAR(128) NOT NULL,
  REL      NVARCHAR(32)  NOT NULL,             -- KSM_OF | ORIGIN_OF | INGREDIENT_OF | PRODUCES | STOCKED_AS | ISSUED_TO
  WEIGHT   DOUBLE,                             -- ORIGIN_OF: import share; PRODUCES: market share; ISSUED_TO: qty
  IS_PROXY NVARCHAR(8)                         -- REAL | PROXY | SYNTH (from the source row)
);

CREATE GRAPH WORKSPACE FF_SUPPLY_GRAPH
  EDGE TABLE FF_G_E SOURCE COLUMN SRC TARGET COLUMN DST KEY COLUMN ID
  VERTEX TABLE FF_G_V KEY COLUMN ID;
