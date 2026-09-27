-- HANA Graph workspace over the supply graph (plan §3 A2, §6). HANA only.
-- FF_G_V / FF_G_E are created by db/schema.sql (S01). A2 needs FF_G_E.WEIGHT (DOUBLE) and FF_G_E.IS_PROXY
-- (NVARCHAR(8)) to use this workspace; until those columns exist it uses NetworkX over the source tables.
-- Run via `python -m ingest.build_graph --ddl` (DROP errors on a first run are ignored).

DROP GRAPH WORKSPACE FF_SUPPLY_GRAPH;

CREATE GRAPH WORKSPACE FF_SUPPLY_GRAPH
  EDGE TABLE FF_G_E SOURCE COLUMN SRC TARGET COLUMN DST KEY COLUMN ID
  VERTEX TABLE FF_G_V KEY COLUMN ID;
