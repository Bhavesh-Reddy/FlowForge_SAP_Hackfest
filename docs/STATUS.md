# STATUS (keep < 60 lines; overwrite, don't append history)

Finale: South & East Region, SRM Chennai, date: ____ (D0)
Updated: 2026-09-26 by Bhavesh

## Owners
- B1 Data & HANA: ____
- B2 Agents & rules: ____
- B3 Platform, UI & action: ____
- P1 Story & deck: ____
- P2 Demo & validation: ____
- Reviewer / merger: Bhavesh

## Stages (plan §13). Edit only your own row: todo → in progress → PR open → merged
| ID | Stage | Owner | State |
|---|---|---|---|
| S00 | Bootstrap | B2 | merged |
| S01 | HANA schema + Day-1 checks | B1 | merged |
| S02 | NPPA + NLEM ingest | B1 | PR open |
| S03 | Cost/origin/NSQ/producers + synth hospital | B1 | todo |
| S04 | A1 Margin Sentinel | B2 | merged |
| S05 | A2 Dependency + graph | B2 | merged |
| S06 | A3 Forecaster | B2 | merged |
| S07 | A4 Resilience + A5 Validator | B2 | merged |
| S08 | A6 Audit/Action + orchestrator | B3 | todo |
| S09 | FastAPI + BTP deploy | B3 | todo |
| S10 | Build Apps guide + fallback UI | B3 | todo |
| S11 | LLM explanation | B3 | todo |
| S12 | Para-19 backtest | B2 + P2 | todo |
| S13 | Integration + demo hardening | Bhavesh | todo |

## Blocked / open questions
- S12 blocked on S02 + S03 (no data/seed/para19_events.csv, no historical price/cost series)
- S01: add FF_G_E.WEIGHT (DOUBLE) + IS_PROXY (NVARCHAR(8)) so A2 can use HANA Graph (see PR feat/harnish-s01-align)
- Does the shared HANA allow CREATE GRAPH WORKSPACE / PAL?
- BTP trial region, and can CF reach HANA eu10?

## Day-1 check results
- 2026-09-27 laptop: HANA connect PASS · create/drop table PASS  | HANA from CF: ?  | Graph: PASS (CREATE GRAPH WORKSPACE allowed)  | PAL: BLOCKED (user has only PUBLIC; PAL call fails with [258] insufficient privilege; organisers won't grant) → A3 uses statsmodels  | API Hub key: ?
