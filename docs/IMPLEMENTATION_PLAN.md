# FlowForge: MVP Implementation Plan
SAP Hackfest 2026 · South & East Region Finale, SRM Chennai · Team PSGITECH169 (FlowForge)
Theme: Resilient Supply Chains · Idea: predict pharmaceutical supply disruptions before they reach patients.

> How to read this: every top-level section starts with `## §n`. Grep for it and read only what you need.
> Tags used below: **[REAL]** public data · **[PROXY]** derived estimate · **[SYNTH]** synthetic, SAP-shaped · **[VERIFY]** check against the primary source before quoting it on stage.

---

## §1 Problem, users and MVP scope

**One-line pitch.** For a medicine under a DPCO price ceiling, the first sign of a shortage is not an empty shelf. It is the input cost rising towards a price the manufacturer is not allowed to raise. FlowForge watches that gap for every scheduled formulation a hospital depends on. It traces how concentrated the supply is, estimates when an exit becomes likely, proposes buffer stock or an alternate source that does not share the same weakness, and routes the proposal to the Chief Pharmacist for approval. Only then does it raise an SAP purchase requisition, and every step is audit-logged.

**Primary users** (validated in our interviews with the PSG Hospitals Director and the PSG Pharmacy Chief Pharmacist):
| User | Needs from FlowForge | Where in the pharmacist's SAP SOP |
|---|---|---|
| Chief Pharmacist | Early warning on vital (V) and NLEM items; recall trace; approve buffer PRs | Demand identification §6, ROL/ROQ §7, PR §8–10, Recall §25, Dashboard §31–32 |
| Purchase Dept | A validated alternate source with a rate at or under the ceiling | Source determination §11, RFQ §12, PO §13 |
| Director / Finance | Budget impact, audit evidence (NABH) | Approval workflow §10/§14, 3-way match §29 |

**The MVP must demo, end to end:**
1. Real NPPA ceiling prices for a focused set of **30–40 NLEM formulations** (old antibiotics, injectables, respiratory, TB/anti-malarial, cardiac: low-price, high-risk). We can say the design scales to ~900 formulations, but we do not load them all.
2. The six agents running in sequence with persisted outputs in SAP HANA Cloud.
3. A watchlist UI plus a **human approval screen** in SAP Build Apps.
4. An approved action becomes an S/4HANA-shaped purchase requisition, with the audit trail shown.
5. A **backtest** on real NPPA Para-19 price-revision events. This is our evidence that the signal works (§4.5).
6. Recall trace (a CDSCO NSQ alert → affected batches across pharmacy locations) as the secondary operational signal the pharmacist asked for.

**Out of scope for the MVP:** real IoT cold chain (simulated), real S/4 posting (sandbox or mock), all ~900 formulations, counterfeit detection, transport rerouting.

---

## §2 Architecture

```
 EXTERNAL (public)                    SAP HANA Cloud (Hackfest-DB, schema = our user)
 NPPA ceiling prices [REAL] ─┐        ┌───────────────────────────────────────────────┐
 NLEM 2022 list      [REAL] ─┤ ingest │ FF_REF_*   regulatory + market reference      │
 TradeStat API imports[REAL]─┼──────▶ │ FF_MM_*    hospital materials/stock (S/4-shaped)│
 WPI index           [REAL] ─┤  (py)  │ FF_GRAPH   supply graph (HANA Graph workspace)│
 CDSCO NSQ alerts    [REAL] ─┤        │ FF_AG_*    agent outputs, approvals, audit    │
 Hospital pharmacy   [SYNTH]─┘        └──────────────────────┬────────────────────────┘
                                                             │ hdbcli / hana-ml
                       SAP BTP (Cloud Foundry, Python buildpack)
             ┌───────────────────────────────────────────────┴──────────────────┐
             │ FastAPI  ── orchestrator ── A1 → A2 → A3 → A4 → A5 ─┐             │
             │                                                     ▼             │
             │        A6 Audit (observer on every hop) ◀── HUMAN GATE ──▶ A6 act │
             │ explain.py (LLM writes the narrative only; numbers are checked)  │
             └───────────────┬───────────────────────────────────────┬──────────┘
                             │ REST (API key)                        │ OData-shaped
                  SAP Build Apps: Watchlist,           S/4HANA API_PURCHASEREQ_PROCESS_SRV
                  Molecule detail, Approval,           (API Hub sandbox read + FF_MM_EBAN mock write)
                  Audit viewer, Recall trace
```

Design principles:
- **Deterministic core.** Scores come from transparent rules. ML is used for forecasting consumption only. The LLM only narrates. (This resolves the deck's contradiction between "XGBoost risk detection" and "deterministic rules".)
- **SAP-native where it adds real value.** HANA holds everything. HANA Graph handles dependencies. HANA PAL does forecasting if the script server is enabled. Build Apps is the UI. S/4 API shapes are used for the action.
- **Offline-safe.** `DB_BACKEND=sqlite` with NetworkX and statsmodels fallbacks, plus a local UI5/HTML fallback page, in case SRM Wi-Fi or the shared HANA fails during the demo.

---

## §3 Agents (A1–A6) and the human gate

All agent inputs and outputs are pydantic models in `agents/contracts.py`. Each agent writes its output row(s) to HANA and emits an audit event through A6.

### A1: Margin Sentinel (Detect)
- **Question:** is making this formulation still viable under its ceiling price?
- **Inputs:** `FF_REF_CEILING_PRICE`, `FF_REF_API_COST_MONTHLY`, `FF_REF_WPI`, `FF_REF_BOM_ASSUMPTION`. Secondary: NSQ alerts, supplier on-time delivery (OTD) from `FF_MM_EKPO` / goods receipts, cold-chain excursions.
- **Logic** (§4.1): compute the estimated manufacturer realisation, the estimated unit cost, and the **headroom %**, plus a 6-month trend and months-to-breach.
- **Output:** `RiskSignal{formulation_id, headroom_pct, headroom_trend, months_to_breach, secondary_signals[], data_quality}` → `FF_AG_SIGNAL`.
- **Trigger:** a nightly batch over all tracked formulations, plus on demand for a **shock scenario** (e.g. "API import cost +30%", the Hormuz/tariff story).

### A2: Dependency Mapper (Assess)
- **Question:** if one producer exits, who is left, and do the remaining producers share one upstream point of failure?
- **Inputs:** `FF_REF_PRODUCER` (formulation → manufacturer), `FF_REF_API_ORIGIN` (API/KSM → import share by country, from TradeStat), `FF_MM_MARA` (which of our hospital materials map to the formulation), and consumption by department.
- **Logic:** traverse the HANA Graph workspace `FF_SUPPLY_GRAPH` (vertices: KSM, API, country, manufacturer, formulation, hospital material, ward). Compute: at-least-N producers, producer HHI, top-country API share, and the downstream set of materials and wards affected.
- **Output:** `DependencyProfile{n_producers_min, hhi, top_origin_country, top_origin_share, affected_materials[], affected_wards[]}` → `FF_AG_DEPENDENCY`.

### A3: Shortage Forecaster (Forecast + stated cause)
- **Question:** when could supply at our hospital fail, and why?
- **Inputs:** A1 and A2 outputs, and hospital stock and consumption (`FF_MM_MCHB`, `FF_MM_MSEG` goods issues).
- **Logic** (§4.3):
  1. **Exit-risk window**: from months-to-breach, adjusted by concentration. Also note the DPCO para 21(2) six-month discontinuation notice: our aim is to raise the flag *before* that notice would be filed.
  2. **Hospital days-of-cover**: usable stock (FEFO, non-expired, non-quarantined) ÷ forecast daily consumption (HANA PAL exponential smoothing, or statsmodels as fallback).
  3. Exposure = the gap between when an exit becomes likely and how long hospital cover plus supplier lead time lasts.
- **Output:** `Forecast{exit_risk_band, window_months_lo/hi, days_of_cover, stockout_date_if_exit, cause_code, cause_facts{}}`. `cause_code` is one of `CEILING_BELOW_COST`, `HEADROOM_ERODING`, `SINGLE_ORIGIN_API`, `FEW_PRODUCERS`, `QUALITY_NSQ`, `SUPPLIER_OTD_DROP`.

### A4: Resilience Simulator (Scenarios + correlated-risk check)
- **Question:** what is the cheapest action that keeps the hospital covered through the risk window without letting stock expire?
- **Options generated:** (a) buffer stock from the current supplier; (b) an alternate manufacturer of the same formulation; (c) a therapeutic alternative from NLEM/the formulary (flagged for clinical review); (d) inter-pharmacy rebalancing (Central → A/B/C Block → Oncology).
- **Correlated-risk check:** reject or penalise any alternate whose API shares the **same origin country or the same KSM node** in the graph. This prevents a "fake fix".
- **Optimisation** (OR-Tools CP-SAT or PuLP): choose quantity per option to minimise cost + stock-out penalty, subject to budget, storage (including cold-chain capacity), **expiry** (quantity ≤ consumption over remaining shelf life − safety margin), and MOQ.
- **Output:** a ranked list of `Scenario{option_type, supplier, qty, cost, coverage_days, expiry_waste_risk, correlated_risk_flag}` → `FF_AG_SCENARIO`.

### A5: Feasibility & Policy Validator (Rules engine; runs before the human sees anything)
Runs `rules/dpco.yaml` and `rules/sop_controls.yaml`. Each check returns PASS / WARN / FAIL with evidence.
| Check | Rule | Source |
|---|---|---|
| Price compliance | unit rate ≤ ceiling price + GST (ceiling excludes GST) | DPCO 2013 para 14 [VERIFY wording] |
| Scheduled-status | formulation is in Schedule I (NLEM 2022); ceiling S.O. number is on record | DPCO Sch. I |
| Supplier licence | vendor master has a valid drug licence number and GST; not blacklisted | SOP §5, CP-04 |
| Quality history | no NSQ alert for this manufacturer/formulation in the last 12 months (WARN if older) | CDSCO NSQ |
| Shelf life | offered residual shelf life ≥ hospital minimum (default 12 months, configurable) | SOP §13, CP-07 |
| Expiry waste | projected expired qty at end of horizon ≤ 5% | SOP §23 |
| Budget | PR value ≤ department budget remaining; value > threshold → second approver | SOP §10 |
| Storage | cold-chain items fit available fridge capacity | SOP §20, CP-08 |
| ROL/ROQ sanity | resulting stock ≤ max-stock × buffer factor | SOP §7, CP-02 |
| Clinical | a therapeutic substitute always needs a pharmacist/clinician sign-off | Formulary policy |
- **Output:** `Recommendation{scenario_id, checks[], overall=READY|NEEDS_CHANGES|BLOCKED, draft_pr{}}`. Only READY or NEEDS_CHANGES items reach the gate.

### 🧑‍⚕️ Human gate: manual verification (the "one approval gate")
- **Who:** the Chief Pharmacist (level 1). Above the ₹ threshold or for a therapeutic substitute, also Procurement / Director (level 2).
- **Screen (SAP Build Apps):** the cause statement, key facts with source labels (REAL/PROXY/SYNTH), the A2 graph snapshot, the chosen scenario and its alternatives, and the A5 check table.
- **Manual verification checklist.** The reviewer must tick every item, and each maps to the pharmacist's control points:
  - [ ] CP-01 material master matches (generic, strength, pack)
  - [ ] CP-02 ROL/ROQ and resulting stock level acceptable
  - [ ] CP-04 supplier/source verified (licence, past performance)
  - [ ] Price ≤ ceiling + GST confirmed against the NPPA notification
  - [ ] Shelf life / FEFO impact acceptable
  - [ ] Cause statement reviewed; this is a risk flag, not a claim about the manufacturer
- **Actions:** Approve · Edit quantity/supplier then approve · Reject (reason required) · Snooze (re-evaluate in N days).
- The decision is written to `FF_AG_APPROVAL` with user, timestamp and checklist state. Rejections become labels for tuning (`rules/weights.yaml`).

### A6: Compliance, Audit & Action
- **Observer role (always on):** every agent hop appends to `FF_AG_AUDIT_LOG` (run_id, agent, input hash, output hash, rule versions, data sources + fetched_at, `PREV_HASH` chain). The table is append-only.
- **Action role (only after approval):** builds the purchase requisition in the exact shape of **S/4HANA `API_PURCHASEREQ_PROCESS_SRV`** (`A_PurchaseRequisitionHeader` / `A_PurchaseReqnItem`: Material, Plant, Quantity, DeliveryDate, PurchasingGroup, FixedSupplier) and posts it to `FF_MM_EBAN` (mock). It also validates master data against the **SAP API Business Hub sandbox** (`API_PRODUCT_SRV`, `API_BUSINESS_PARTNER`) read-only.
- **Reports:** a per-decision audit PDF/HTML (NABH evidence: who, what, why, sources) and a "Form-IV watch" (formulations with elevated exit risk).
- **Blocks** any action without an approval row, and any hash-chain break.

---

## §4 Scoring rules (deterministic, explainable)

All constants live in `rules/weights.yaml` and `rules/dpco.yaml`, with their source noted.

### 4.1 Margin headroom (A1)
```
CP            = NPPA ceiling price per unit (excl. GST)                         [REAL]
PTR           = CP / (1 + retailer_margin)        retailer_margin = 0.16         (DPCO para 4 basis) [VERIFY]
realisation   = PTR / (1 + wholesaler_margin)     wholesaler_margin = 0.10 (assumption, configurable)
unit_cost     = api_cost_per_kg × api_g_per_unit / 1000 / yield
              + conversion_cost + packaging_cost + freight_cost                  [PROXY]
headroom_pct  = (realisation − unit_cost) / realisation
```
- The API cost per kg comes from the TradeStat import **unit value** (value ÷ quantity) for the API's HS 8-digit code, as a 3-month moving median [PROXY from REAL]. Conversion and packaging costs are indexed to WPI (manufactured products) [PROXY].
- **Trend:** OLS slope of headroom_pct over the last 6–12 months. `months_to_breach = headroom_pct / −slope` (only if slope < 0).
- **Signal bands:** `headroom < 5%` or `months_to_breach ≤ 6` → RED; `< 15%` or `≤ 12` → AMBER.

### 4.2 Concentration (A2)
`conc = 0.5·(1/n_producers_min) + 0.3·HHI_producers + 0.2·top_origin_share`, bounded to [0,1].

### 4.3 Exit risk and exposure (A3)
```
exit_risk = sigmoid( a·(−headroom_pct) + b·(1/months_to_breach) + c·conc + d·nsq_recent + e·otd_drop )
exposure  = exit_risk × criticality × clamp( (lead_time_days + requal_days) / max(days_of_cover,1), 0, 3 )
criticality: VED V=1.0, E=0.6, D=0.3; ×1.2 if NLEM core/primary level
```
- Start with hand-set coefficients. Tune them once, on the backtest (§4.5), and freeze them before the finale. Do not overfit.
- **Confidence** = share of inputs that are REAL rather than PROXY/SYNTH, times data freshness. It is shown next to every score.

### 4.4 Watchlist ranking
Sort by `exposure`, tie-break by `confidence`. Show the top 10 with cause chips.

### 4.5 Validation: the Para-19 backtest (our strongest slide)
NPPA has used DPCO para 19 (extraordinary circumstances) to **raise** ceiling prices of formulations that manufacturers reported as unviable. Those events are real "silent-disruption" labels.
- Positive set **[VERIFY exact list from the NPPA office memoranda]**: the Oct 2024 increase of ~50% for ~11 formulations of 8 drugs (e.g. benzylpenicillin inj., atropine inj., streptomycin inj., salbutamol tab/respirator solution, pilocarpine drops, cefadroxil tab, desferrioxamine inj., lithium carbonate tab), and the Dec 2019 increase for ~21 formulations (e.g. BCG vaccine, chloroquine, dapsone, metronidazole, vitamin C). Also include any 2025–26 para-19 orders you find.
- Negatives: randomly sampled scheduled formulations with no para-19 action in the same window.
- Method: replay A1–A3 month by month using only data available at that time (no look-ahead). Report the **median lead time** (first AMBER/RED → NPPA order date), **precision@10** and **recall**.
- Be honest: the cost side is a proxy, so present this as "directional evidence", not a validated model.

---

## §5 Data sources (what is real, what is not)

| Dataset | Source | Tag | Ingest script | Notes |
|---|---|---|---|---|
| Ceiling prices (scheduled formulations, S.O. no., date) | NPPA website: ceiling price compendium / notifications (nppaindia.nic.in) | REAL | `ingest/nppa.py` | xlsx/PDF → tabula/pdfplumber; store S.O. number + date |
| Annual WPI revision % (1 April each year) | NPPA office memoranda | REAL | `ingest/nppa.py` | [VERIFY] 2022 +10.76%, 2023 +12.12%, 2024 +0.0055%, 2025 +1.74% |
| Para-19 orders | NPPA office memoranda | REAL | `ingest/nppa_para19.py` | backtest labels |
| NLEM 2022 (384 medicines) | MoHFW / CDSCO | REAL | `ingest/nlem.py` | dosage form, strength, level of care |
| API import value & qty by HS8 × country × month | Ministry of Commerce TradeStat (tradestat.commerce.gov.in) | REAL → PROXY cost | `ingest/tradestat.py` | map API → HS8 by hand for the 30–40 targets; the origin share feeds A2 |
| WPI (manufactured products, chemicals) | Office of the Economic Adviser (eaindustry.nic.in) | REAL | `ingest/wpi.py` | cost indexation |
| NSQ / spurious drug alerts | CDSCO monthly drug alerts | REAL | `ingest/nsq.py` | manufacturer, batch, drug → recall trace + quality signal |
| Producers per formulation | NPPA price notifications (company names), CDSCO approvals, Jan Aushadhi / BPPI supplier lists | REAL (lower bound) | `ingest/producers.py` | always shown as "at least N" |
| BOM assumptions (API mg/unit, yield, conversion cost) | label strength + literature defaults | PROXY | `rules/bom_defaults.yaml` | documented per molecule |
| Hospital pharmacy: materials, vendors, stock by batch, goods movements, POs, PRs, cold-chain logs | generator shaped on the PSG Chief Pharmacist's SAP SOP | SYNTH | `ingest/synth_hospital.py` | seeded; 5 locations (Central, A, B, C Block, Oncology); 18 months |

Every table gets `SOURCE, SOURCE_URL, FETCHED_AT, IS_PROXY` columns. Raw downloads go in `data/raw/` (gitignored); cleaned seeds (small CSVs) go in `data/seed/` (committed).

---

## §6 SAP HANA Cloud data model (schema = our DB user; all objects `FF_`)

**Reference (external):**
- `FF_REF_FORMULATION(FORM_ID PK, GENERIC, DOSAGE_FORM, STRENGTH, UNIT, NLEM_LEVEL, THERAPEUTIC_CLASS)`
- `FF_REF_CEILING_PRICE(FORM_ID, EFFECTIVE_FROM, CEILING_PRICE, SO_NUMBER, PARA {4|16|19}, …provenance)`
- `FF_REF_API(API_ID, NAME, HS8, KSM_ID)`, `FF_REF_FORM_API(FORM_ID, API_ID, API_MG_PER_UNIT)`
- `FF_REF_API_COST_MONTHLY(API_ID, MONTH, UNIT_VALUE_INR_KG, QTY_KG, IS_PROXY)`
- `FF_REF_API_ORIGIN(API_ID, MONTH, COUNTRY, SHARE)`
- `FF_REF_PRODUCER(FORM_ID, MANUFACTURER_ID, EVIDENCE_SOURCE)`, `FF_REF_MANUFACTURER(ID, NAME, STATE, LICENCE_NO)`
- `FF_REF_WPI(MONTH, SERIES, INDEX_VALUE)`, `FF_REF_NSQ_ALERT(MONTH, DRUG, BATCH, MANUFACTURER_ID, REASON)`

**Hospital MM (S/4-shaped, SYNTH). Column names mirror the standard tables so a real S/4 swap is mechanical:**
- `FF_MM_MARA` (MATNR, generic, strength, VED, ABC, FSN, storage condition, batch-managed flag, FORM_ID link)
- `FF_MM_LFA1` (LIFNR, name, GSTIN, drug licence no, blacklist flag) · `FF_MM_T001W` (plants/locations)
- `FF_MM_MCHB` (MATNR, WERKS, LGORT, CHARG, CLABS qty, VFDAT expiry, status unrestricted/quarantine)
- `FF_MM_MSEG` (movement types 101 GR, 261/201 GI to ward, 311 transfer, 122 return, 551 scrap; date, qty, batch)
- `FF_MM_EKKO/EKPO` (POs incl. delivery date vs actual → OTD) · `FF_MM_EBAN` (PRs; A6 writes here)
- `FF_MM_COLDCHAIN(LOCATION, TS, TEMP_C, EXCURSION_FLAG)`

**Agent layer:** `FF_AG_RUN`, `FF_AG_SIGNAL`, `FF_AG_DEPENDENCY`, `FF_AG_FORECAST`, `FF_AG_SCENARIO`, `FF_AG_RECOMMENDATION`, `FF_AG_CHECK`, `FF_AG_APPROVAL`, `FF_AG_AUDIT_LOG` (append-only, `PREV_HASH`, `ROW_HASH`).

**Views** (`db/views.sql`): `FF_V_WATCHLIST`, `FF_V_DAYS_OF_COVER` (FEFO usable stock ÷ avg daily issue), `FF_V_SUPPLIER_OTD`, `FF_V_RECALL_TRACE` (NSQ batch ↔ MCHB across locations).

**Graph** (`db/graph.sql`): vertex table `FF_G_V(ID, TYPE, LABEL)` and edge table `FF_G_E(ID, SRC, DST, REL)`, then `CREATE GRAPH WORKSPACE FF_SUPPLY_GRAPH …`. Test on day 1 whether the shared instance allows it; if not, use the NetworkX fallback over the same tables.

---

## §7 SAP tools: what we use and how (access given by the organisers + trials)

| SAP asset | Access | Use in FlowForge | Day-1 check |
|---|---|---|---|
| **SAP HANA Cloud** (Hackfest-DB, eu10, user `HACKFEST0xxx`) | organiser landscape; shared password | System of record for all tables, views, audit; Graph workspace; PAL forecasting via `hana-ml` | Connect with `hdbcli` from a laptop (port 443, TLS). Check `CREATE TABLE` rights, `CREATE GRAPH WORKSPACE`, and `SELECT * FROM SYS.AFL_AREAS` (PAL) |
| **SAP HANA Database Explorer** | via HANA Cloud Central → Open | Run DDL, inspect data, show the SQL console to judges | — |
| **SAP BTP trial** (Cloud Foundry) | self-registered trial | Host the FastAPI orchestrator (`cf push`, Python buildpack); user-provided service holding HANA creds | Region (us10/ap21) and memory quota; outbound to HANA eu10 allowed |
| **SAP Build Apps** | BTP trial (SAP Build booster) | Watchlist, molecule detail, approval checklist, audit viewer, recall trace; calls FastAPI via a BTP destination | Build a hello-world that calls a public REST endpoint |
| **SAP Build Process Automation** | BTP trial | *Stretch:* the approval as a formal workflow with a 2-level approver. The MVP does approval inside Build Apps → FastAPI | Only if time allows |
| **SAP API Business Hub sandbox** | api.sap.com (free API key) | Read-only master-data validation (`API_PRODUCT_SRV`, `API_BUSINESS_PARTNER`); PR payload shaped on `API_PURCHASEREQ_PROCESS_SRV` | GET `A_Product?$top=1` with the APIKey header |
| **SAP Business Data Cloud trial** | self-registered | Not in the MVP. Mention as the production path for combining S/4 + external data products | — |
| **Joule / SAP Generative AI Hub** | not available to us | Say "designed to plug into Generative AI Hub / Joule". The MVP uses `LLM_PROVIDER=anthropic` or template-only | Don't claim Joule in the demo |

**Environment variables** (keep in a gitignored `.env` file; never commit or paste them): `DB_BACKEND (hana|sqlite)`, `HANA_HOST`, `HANA_PORT=443`, `HANA_USER`, `HANA_PASSWORD`, `HANA_SCHEMA`, `SAP_API_HUB_KEY`, `LLM_PROVIDER (anthropic|sap_genai_hub|none)`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `APP_API_KEY` (header shared by Build Apps → FastAPI).

**Shared-instance safety:** all participants share one password, so anyone can log in as our user. Keep every load idempotent (`python -m ingest.load_hana --reset` rebuilds the schema in < 5 min), take a nightly `data/seed` export, and have the sqlite fallback ready.

---

## §8 Repository layout
```
CLAUDE.md                      .claude/skills/flowforge/SKILL.md   .claude/settings.json
docs/ IMPLEMENTATION_PLAN.md  STATUS.md  DECISIONS.md  demo_script.md
db/ schema.sql  views.sql  graph.sql  seed_small.sql
ingest/ nppa.py nppa_para19.py nlem.py tradestat.py wpi.py nsq.py producers.py synth_hospital.py load_hana.py
rules/ dpco.yaml  sop_controls.yaml  weights.yaml  bom_defaults.yaml
agents/ contracts.py  ctx.py  a1_margin_sentinel.py  a2_dependency.py  a3_forecast.py
        a4_resilience.py  a5_validator.py  a6_audit.py  explain.py  orchestrator.py
api/ main.py  (routes: /watchlist /molecule/{id} /run /scenario/shock /approval /audit/{run} /recall/{alert})
ui/ buildapps/ (exported project)   fallback/ index.html (UI5 CDN or plain JS)
tests/ test_rules.py  test_a1.py  test_audit_chain.py  backtest_para19.py
scripts/ git-hooks/commit-msg   deploy_cf.sh   export_seed.py
manifest.yml  requirements.txt  .gitignore
```

---

## §9 Team split and timeline

**Roles (3 builders + 2 on story/deck).** Fill in names in `docs/STATUS.md`.
| Role | Owns | Hand-off contract |
|---|---|---|
| **B1: Data & HANA** | ingest/*, db/*, HANA Graph, synthetic hospital data, recall view | tables populated + `FF_V_*` views by end of Day 1 |
| **B2: Agents & rules** | agents/a1–a5, rules/*.yaml, forecasting, OR-Tools, backtest | `orchestrator --dry-run` green on 5 molecules by end of Day 2 |
| **B3: Platform, UI & action** | FastAPI, BTP CF deploy, Build Apps screens, approval, A6 audit/action, API Hub | Build Apps calling live API by end of Day 2 |
| **P1: Story & deck** | deck rewrite (§12), citations check, architecture slide from §2 | deck v2 by Day 2 evening |
| **P2: Demo & validation** | demo script (§10), backtest numbers into slides, backup video, Q&A sheet, pharmacist quote/photos consent | full dry-run recording by Day 3 |

**Timeline** (D0 = finale day; set the date in STATUS.md):
| Day | Builders | Deck pair | Exit criterion |
|---|---|---|---|
| **D-4** | B1: HANA connect + DDL + NPPA/NLEM ingest for 30–40 targets. B2: contracts.py, A1 on seed CSV. B3: FastAPI skeleton, CF trial push, Build Apps hello-world | Freeze story; start deck v2; request the pharmacist's permission to quote | HANA reachable from laptop **and** CF; A1 numbers for 5 molecules |
| **D-3** | B1: TradeStat + WPI + NSQ + producers; synth hospital; graph. B2: A2, A3, A5 rules. B3: watchlist + detail screens, audit chain | Architecture + agent slides; KPI slide placeholder | All 5 agents dry-run; watchlist live on Build Apps |
| **D-2** | B1: recall view, seed export, sqlite fallback. B2: A4 optimiser + correlated check; backtest. B3: approval screen + checklist, A6 PR mock + API Hub validation, explain.py | Insert backtest results; demo script v1 | Full end-to-end: shock → watchlist → approve → PR + audit |
| **D-1** | All: bug bash, freeze rules/weights, rehearse 3×, record backup video, test on a phone hotspot | Final deck; Q&A sheet; print one-page leave-behind | Demo < 6 min, 2 clean runs in a row |
| **D0** | Pre-flight: HANA up, CF app awake, `--reset` tested, sqlite ready | Present | — |

**Cut list if behind** (drop in this order): Build Process Automation → therapeutic-alternative option → HANA PAL (use statsmodels) → HANA Graph (use NetworkX) → API Hub validation. **Never cut:** A1 on real NPPA data, the human gate, the audit trail, the backtest slide.

---

## §10 Finale demo script (≈6 minutes)
1. **Hook (30s):** a pharmacy shelf photo from our PSG visit. "The first sign of a shortage isn't a shortage."
2. **Mechanism (45s):** the ceiling-vs-cost chart for one real formulation: a fixed NPPA ceiling, and API import cost (TradeStat) rising towards it.
3. **Live run (2m):** press *Shock: API import cost +30%* (the Hormuz/freight/tariff story from the theme brief). A1 moves 4 molecules to RED. Open one: A2 shows "at least 3 producers, 72% of API from one country". A3: "exit-risk window 4–7 months; hospital cover 21 days; cause: ceiling below estimated cost".
4. **Fake-fix catch (30s):** A4's cheapest alternate is rejected because it shares the same API origin; the next-best is chosen with buffer quantity sized to avoid expiry waste.
5. **Human gate (45s):** the Chief Pharmacist view. A5 shows all checks green (price ≤ ceiling + GST, licence, shelf life). Tick the checklist → Approve.
6. **Action + audit (30s):** the S/4-shaped PR appears; the audit chain shows every hop with sources and hashes (NABH evidence).
7. **Recall bonus (20s):** a CDSCO NSQ alert → the affected batch is located in 3 pharmacy locations in one click (the pharmacist's CP-14).
8. **Proof (30s):** the backtest slide: median X months of lead time before real NPPA para-19 revisions. Close on scalability and the B2B model.

---

## §11 Risks and mitigations
| Risk | Mitigation |
|---|---|
| Shared HANA password; another team drops or edits our tables | idempotent `--reset`, seed export, sqlite fallback, verify row counts pre-demo |
| HANA not reachable from BTP CF (allowlist) or eu10 latency | test on D-4; if blocked, run FastAPI locally and point Build Apps at a tunnel, or use the fallback UI |
| NPPA data in PDF, messy | restrict to 30–40 targets; hand-verify each row; store S.O. number as proof |
| TradeStat HS8 ↔ API mapping ambiguous | pick APIs with a dedicated HS8 line; label PROXY; show confidence |
| Judges question the proxy cost model | show the formula, assumptions file, sensitivity (±20%), and backtest honesty |
| Build Apps learning curve | B3 starts D-4; fallback HTML/UI5 page on the same API |
| Venue Wi-Fi | phone hotspot, sqlite + local LLM-off mode (`LLM_PROVIDER=none`), backup video |
| Over-claiming | never "Joule-powered", never "predicts manufacturer intent", never "real S/4 posting" |

---

## §12 Deck fixes for the PPT pair (current PDF → v2)
1. **Slide 2:** the five "why" questions (transport, overstock, cold chain, recall, counterfeit) dilute the thesis. Keep one line, and lead with the margin-squeeze mechanism. **Verify the Reuters "June 12, 2026" cancer-drug headline**; if it can't be sourced, remove it.
2. **Slide 3 (ground validation):** add who was met (PSG Hospitals Director, PSG Pharmacy Chief Pharmacist) and 2–3 concrete insights (the SAP pharmacy SOP, recall trace, FEFO/expiry, control points). Get consent before showing faces.
3. **Slide 4:** good. Show the headroom formula under the chart.
4. **Slide 5 (workflow):** rename the agents to match §3 (A1 Margin Sentinel … A6 Compliance/Audit/Action). Move the **human gate before A6's action**, and show A6 as audit-on-every-hop.
5. **Slide 6:** replace **DSCSA** (US law) with Indian references: DPCO 2013 paras 14/16/19/21, CDSCO NSQ alerts, the QR-code/track-and-trace mandate for APIs & top brands [VERIFY], and revised Schedule M GMP [VERIFY].
6. **Slide 8:** remove the duplicated blocks. Replace "Joule" with "GenAI explanation layer (Generative AI Hub-ready)". Remove XGBoost/Random-Forest risk detection (scores are rule-based); keep forecasting as PAL/statsmodels. Show the SAP tools table from §7.
7. **Slide 9:** replace "907–928" with one sourced number [VERIFY]. Add the backtest KPIs (lead time, precision@10).
8. **Slide 10:** replace the generic organiser roadmap with our own §9 timeline.
9. **Slide 11:** "No real-user validation yet" is now outdated: we met the Chief Pharmacist and the Director. Change it to "Validated with PSG Pharmacy; pilot on their SAP data proposed".
10. **Slide 12 (related work):** delete the leftover assistant notes: "(Read the paper before quoting specifics.)" and "(Figures as cited earlier in your deck.)". Verify every citation (arXiv 2601.09680, Pall et al. 2023, Frontiers 2025, Liu et al. AJHP 2021, USP report 2026) before the finale. Drop any that can't be verified.
11. **Throughout:** tag every number REAL / PROXY / SYNTH, the same way the product does.
