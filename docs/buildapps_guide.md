# SAP Build Apps: FlowForge build guide (S10)

This guide builds the five FlowForge pages in SAP Build Apps on top of the FlowForge API (`api/main.py`, deployed in S09). Everything here is done by clicking in the browser.

The API also serves a working fallback of the same pages at `https://<route>/ui` (file: `ui/fallback/index.html`). Use it as the visual reference while you build, and as the backup screen for the demo.

**Wording rule for every label you type.** A flag is a "risk flag for review". Never write that a manufacturer "will exit", "will discontinue" or "is leaving". Use: "margin headroom below threshold; exit risk elevated". Always write producer counts as "at least N producers".

Contents:
- 0. Before you start
- 1. BTP destination
- 2. Data resources, one per endpoint
- 3. App variables and theme
- 4. Pages 1–5: Watchlist, Molecule detail, Approval, Audit, Recall trace
- 5. The "Shock +30%" button
- 6. Demo checklist

---

## 0. Before you start

- The API runs on Cloud Foundry (see `docs/deploy.md`). Note its route, for example `https://flowforge-api-xyz.cfapps.us10-001.hana.ondemand.com`.
- Keep the `app_api_key` you put into the `ff-hana` service to hand.
- Your BTP trial subaccount needs the SAP Build Apps booster (SAP Build → Build Apps). Open the SAP Build lobby and create a **Web & Mobile application** called `FlowForge`.

Formula names below are the ones in the Build Apps formula editor. If your version's editor names a function differently, search the function list; each description says what the result must be.

---

## 1. BTP destination

BTP cockpit → your subaccount → **Connectivity → Destinations → New Destination**:

| Field | Value |
|---|---|
| Name | `FLOWFORGE_API` |
| Type | `HTTP` |
| URL | `https://<route>`, with no trailing slash |
| Proxy Type | `Internet` |
| Authentication | `NoAuthentication` |

Under **Additional Properties**, click *New Property* for each row:

| Property | Value | Why |
|---|---|---|
| `HTML5.DynamicDestination` | `true` | lets Build Apps runtime call it |
| `WebIDEEnabled` | `true` | shows it in design tools |
| `AppgyverEnabled` | `true` | shows it in Build Apps' destination picker |
| `HTML5.Timeout` | `120000` | `POST /run` can take up to ~1–2 min |
| `URL.headers.X-API-Key` | `<app_api_key>` | the API key header, injected server-side so it never ships in the app |

Save, then click **Check Connection**. A `200`, `401` or `404` all mean it's reachable. `/health` itself needs no key.

> Because the key lives in the destination, the Build Apps app never contains it. Only people who can open the app through BTP can call the API.

---

## 2. Data resources, one per endpoint

Build Apps → **Data** tab → **Create data entity → SAP BTP destination REST API integration** → pick `FLOWFORGE_API`. Create one entity per row below.

In each entity:
1. Set the **Resource schema**. Click *Test* with the sample input, then **Set schema from response**.
2. Enable only the method listed in the table, and set its **Relative path**.

| Data entity | Method | Relative path | Test input | Returns |
|---|---|---|---|---|
| `Watchlist` | GET collection | `/watchlist?limit=200&assessed_only=true&sort=exposure` | – | list of rows (below) |
| `Molecule` | GET record | `/molecule/{id}` (path param `id`) | `id` = a form id from Watchlist | one object |
| `MarginSeries` | GET record | `/molecule/{id}/margin-series?months=12&shock=1.3` | same `id` | `{points: [...]}` |
| `ShockWhatIf` | POST (create) | `/scenario/shock` | body `{"multiplier": 1.3}` | `{rows: [...]}` |
| `Run` | POST (create) | `/run` | body `{"form_ids": ["<id>"], "shock": 1.3}` | `{run_id, awaiting_approval: [...]}` |
| `PendingApprovals` | GET collection | `/approvals/pending` | – | list |
| `Approval` | POST (create) | `/approval` | body, see page 3 | `{status, banfn, payload}` |
| `Audit` | GET record | `/audit/{run_id}` | a `run_id` from Run | `{chain, events: [...]}` |
| `Recall` | GET record | `/recall/{alert_id}` | an NSQ alert id | `{alert, matches: [...]}` |

Headers: don't add `X-API-Key` here, because the destination injects it. For POST entities, add `Content-Type: application/json`.

**Watchlist row fields** used below: `form_id, generic, strength, dosage_form, exit_risk_band, exposure, cause_code, window_months_lo, window_months_hi, headroom_pct, producers_label, min_days_of_cover, confidence, data_tag, run_id`.

---

## 3. App variables and theme

**App variables** (Variables → App variables):

| Name | Type | Initial |
|---|---|---|
| `selectedFormId` | text | `""` |
| `selectedRecId` | text | `""` |
| `lastRunId` | text | `""` |
| `approverId` | text | `""` |

**Theme** (Theme → Colors). Status colours are reserved for flags and are always paired with an icon and a word:

| Role | Hex |
|---|---|
| Critical (RED / FAIL) | `#d03b3b` |
| Warning (AMBER / WARN) | `#fab219` |
| Good (GREEN / PASS) | `#0ca30c` |
| Chart series 1 (realisation) | `#2a78d6` |
| Chart series 2 (unit cost) | `#eb6834` |

For the projector: base font size 18 and a light theme.

---

## 4. Pages

### Page 1: Watchlist (home page)

**Page variable** `rows` (data variable of type `Watchlist`, collection):
- Logic: *Page mounted* → **Get record collection** (`Watchlist`) → **Set page variable** `rows` = `outputs["Get record collection"].records`.
- Add a **Pull-to-refresh** or a *Refresh* button with the same flow.

The API already sorts by exposure (A3: exit risk × criticality × gap in hospital cover), highest first. Don't re-sort in the app.

**Layout:**
1. Title text `Watchlist`. Subtitle text `Sorted by exposure. Every flag is a risk flag for review, not a statement about any manufacturer.`
2. **List item** repeated with `pageVars.rows` (Repeat → `pageVars.rows`). In each item:

| Field | Component | Binding (formula) |
|---|---|---|
| Name | Title text | `repeated.current.generic + " " + repeated.current.strength` |
| Form | Subtitle text | `repeated.current.dosage_form` |
| Risk badge | Text in a container | `IF(repeated.current.exit_risk_band == "RED", "▲ High (RED)", IF(repeated.current.exit_risk_band == "AMBER", "◆ Elevated (AMBER)", IF(repeated.current.exit_risk_band == "GREEN", "● Low (GREEN)", "not assessed")))` |
| Badge colour | Container background | `IF(repeated.current.exit_risk_band == "RED", "#d03b3b", IF(repeated.current.exit_risk_band == "AMBER", "#fab219", "#0ca30c"))` |
| Exposure | Text | `IF(IS_EMPTY(repeated.current.exposure), "–", FORMAT_LOCALIZED_DECIMAL(repeated.current.exposure, "en", 2))` |
| Cause chip | Text in a pill container | see the cause-label formula below |
| Window | Text | `IF(IS_EMPTY(repeated.current.window_months_lo), "–", ROUND(repeated.current.window_months_lo, 0) + "–" + ROUND(repeated.current.window_months_hi, 0) + " months")` |
| Headroom | Text | `IF(IS_EMPTY(repeated.current.headroom_pct), "–", FORMAT_LOCALIZED_DECIMAL(repeated.current.headroom_pct * 100, "en", 1) + "%")` |
| Producers | Text | `repeated.current.producers_label` (already "at least N producers") |
| Confidence | Text | `IF(IS_EMPTY(repeated.current.confidence), "–", ROUND(repeated.current.confidence * 100, 0) + "% real data")` |
| Data badge | Text with border | `repeated.current.data_tag`; outline colour green for `REAL`, dashed for `PROXY`, dotted for `SYNTH` |

**Cause-label formula:**
```
IF(repeated.current.cause_code == "CEILING_BELOW_COST", "Ceiling below est. cost",
IF(repeated.current.cause_code == "HEADROOM_ERODING", "Headroom eroding",
IF(repeated.current.cause_code == "SINGLE_ORIGIN_API", "Single-origin API",
IF(repeated.current.cause_code == "FEW_PRODUCERS", "Few producers",
IF(repeated.current.cause_code == "QUALITY_NSQ", "Quality alerts (NSQ)",
IF(repeated.current.cause_code == "SUPPLIER_OTD_DROP", "Supplier delivery drop", "–"))))))
```

**Tap a row:** *Component tap* → **Set app variable** `selectedFormId` = `repeated.current.form_id` → **Open page** `Molecule detail`.

Put the **Shock +30%** button (section 5) in the top bar of this page.

### Page 2: Molecule detail

**Page variables:** `m` (type `Molecule`, record) and `series` (type `MarginSeries`, record).

Logic on *Page mounted*:
1. **Get record** `Molecule` with `id` = `appVars.selectedFormId` → set `m`.
2. **Get record** `MarginSeries` with `id` = `appVars.selectedFormId` → set `series`. This call can take a few seconds because the API replays A1 month by month, so show a spinner until it resolves.

**Sections:**

1. **Header:** `pageVars.m.formulation.GENERIC + " " + pageVars.m.formulation.STRENGTH`. Subtitle: `"Ceiling ₹" + pageVars.m.formulation.CEILING_PRICE + " excl. GST · " + pageVars.m.formulation.CEILING_SO_NUMBER`. Under it, the disclaimer text `pageVars.m.disclaimer`.
2. **Ceiling vs estimated cost (chart).** Install **Line chart** from the component marketplace.
   - Data: `pageVars.series.points`, x-axis `month`.
   - Series 1 `realisation_inr`, colour `#2a78d6`, label "Est. manufacturer realisation under the NPPA ceiling".
   - Series 2 `unit_cost_inr`, colour `#eb6834`, label "Est. unit cost".
   - Series 3 `shocked_unit_cost_inr`, colour `#eb6834`, dashed, label "Est. unit cost if API import cost ×1.3".
   - Rules: one y-axis only (₹), legend on, and leave months with null values blank; don't draw them as zero.
   - Caption: `pageVars.series.note`.
3. **Margin (A1):**

   | Label | Binding |
   |---|---|
   | Flag | `pageVars.m.signal.band` (same badge formula as the watchlist) |
   | Headroom | `FORMAT_LOCALIZED_DECIMAL(pageVars.m.signal.headroom_pct * 100, "en", 1) + "%"` |
   | Months to breach | `IF(IS_EMPTY(pageVars.m.signal.months_to_breach), "–", ROUND(pageVars.m.signal.months_to_breach, 1))` |

4. **Dependency summary (A2):**

   | Label | Binding |
   |---|---|
   | Producers | `"at least " + pageVars.m.dependency.n_producers_min + " producers"` |
   | Top API origin | `pageVars.m.dependency.top_origin_country + " · " + ROUND(pageVars.m.dependency.top_origin_share * 100, 0) + "% of imports"` |
   | Affected wards | `JOIN(pageVars.m.dependency.affected_wards, ", ")` |

   Optional: a list repeated over `pageVars.m.graph.edges`, showing `rel`, `src`, `dst` and `is_proxy`.
5. **Forecast (A3):**

   | Label | Binding |
   |---|---|
   | Exit-risk flag | `pageVars.m.forecast.exit_risk_band` (badge formula) |
   | Cause | cause-label formula applied to `pageVars.m.forecast.cause_code` |
   | Window | `ROUND(pageVars.m.forecast.window_months_lo, 0) + "–" + ROUND(pageVars.m.forecast.window_months_hi, 0) + " months"` |
   | Hospital cover | `ROUND(pageVars.m.forecast.days_of_cover, 0) + " days"` |

   Fixed caption: "When an exit becomes more likely, not a prediction that it will happen."
6. **Scenarios (A4) + verdict (A5):** a list repeated over `pageVars.m.scenarios`:

   | Field | Binding |
   |---|---|
   | Rank | `IF(IS_EMPTY(repeated.current.rank), "rejected (correlated risk)", "#" + repeated.current.rank)` |
   | Option | `repeated.current.option_type` |
   | Supplier | `repeated.current.supplier` |
   | Qty | `repeated.current.qty` |
   | Cost | `"₹" + ROUND(repeated.current.cost, 0)` |
   | Coverage | `ROUND(repeated.current.coverage_days, 0) + " days"` |
   | Correlated risk | `IF(repeated.current.correlated_risk_flag, "✕ shares API origin / KSM", "none")` |

   A5 verdict: `SELECT(pageVars.m.recommendations, item.recommendation.scenario_id == repeated.current.scenario_id)[0].recommendation.overall`.

   **Review** button, visible when the verdict is not `BLOCKED`: set `selectedRecId` = `repeated.current.scenario_id` → **Open page** `Approval`.
7. **Audit trail** button: set `lastRunId` = `pageVars.m.run_id` → **Open page** `Audit`.

### Page 3: Approval (Chief Pharmacist gate)

**Page variables:** `queue` (`PendingApprovals` collection); `sel` (object, one pending item); six booleans `ck1` to `ck6` (initial `false`); `reason` (text); `editQty` (number); `editSupplier` (text); `snoozeDays` (number, initial `7`); `result` (object).

*Page mounted* → **Get record collection** `PendingApprovals` → set `queue` → set `sel` = `IF(IS_EMPTY(appVars.selectedRecId), queue[0], SELECT(queue, item.rec_id == appVars.selectedRecId)[0])`.

**Left column:** list over `pageVars.queue` with title `repeated.current.generic` and subtitle `repeated.current.scenario.option_type + " · ₹" + ROUND(repeated.current.scenario.cost, 0) + " · level " + repeated.current.awaiting_level`. Tap → set `sel` = `repeated.current`.

**Right column:**

1. **Summary.** Bind these to `pageVars.sel.draft_pr.*`: Material, Plant, RequestedQuantity + BaseUnit, FixedSupplier, PurchaseRequisitionPrice ("incl. GST"), DeliveryDate. Show value `pageVars.sel.scenario.cost`. If `pageVars.sel.needs_second_approver`, show the chip "second approver required".
2. **A5 check table:** list over `pageVars.sel.checks`.

   | Column | Binding |
   |---|---|
   | Result | `IF(repeated.current.status == "PASS", "✓ PASS", IF(repeated.current.status == "WARN", "! WARN", "✕ FAIL"))`, background from the status colours |
   | Check | `repeated.current.name` |
   | Rule | `repeated.current.evidence.rule` |
   | Source | `repeated.current.rule_source` |

3. **Manual verification checklist.** Six checkboxes bound to `ck1`–`ck6`, with these exact labels:
   1. CP-01 Material master matches (generic, strength, pack)
   2. CP-02 ROL/ROQ and the resulting stock level are acceptable
   3. CP-04 Supplier / source verified (licence, past performance)
   4. Price ≤ ceiling + GST confirmed against the NPPA notification
   5. Shelf life / FEFO impact acceptable
   6. Cause statement reviewed: this is a risk flag, not a claim about the manufacturer
4. **Approver** input bound to `appVars.approverId`.
5. **Buttons:**
   - **Approve.** Disabled when `NOT(pageVars.ck1 && pageVars.ck2 && pageVars.ck3 && pageVars.ck4 && pageVars.ck5 && pageVars.ck6)`. Tap → **Create record** `Approval` with this body, then set `result` = the output:
     ```
     {"rec_id": pageVars.sel.rec_id, "decision": "APPROVE", "user": appVars.approverId,
      "level": pageVars.sel.awaiting_level,
      "checklist": {"cp01_material_master": pageVars.ck1, "cp02_rol_roq_stock": pageVars.ck2,
                    "cp04_supplier_verified": pageVars.ck3, "price_within_ceiling_gst": pageVars.ck4,
                    "shelf_life_fefo_ok": pageVars.ck5, "cause_reviewed_not_claim": pageVars.ck6}}
     ```
   - **Edit & approve.** Same condition, plus inputs `editQty` and `editSupplier`. Body is as above with `"decision": "EDIT_APPROVE"` and `"edits": {"qty": pageVars.editQty, "supplier": pageVars.editSupplier}`.
   - **Reject.** A text area bound to `reason`. The button is disabled when `IS_EMPTY(TRIM(pageVars.reason))`. Body: `{"rec_id": …, "decision": "REJECT", "user": …, "reason": pageVars.reason}`.
   - **Snooze.** A number input bound to `snoozeDays`. Body: `{"rec_id": …, "decision": "SNOOZE", "user": …, "snooze_days": pageVars.snoozeDays}`.
6. **Result.**
   - Show `pageVars.result.status`.
   - If `pageVars.result.banfn` is set, show `"Purchase requisition " + pageVars.result.banfn + " created (S/4-shaped, FF_MM_EBAN mock)"`, plus a multi-line text with `ENCODE_JSON(pageVars.result.payload)`.
   - Show the list of `pageVars.result.notes`.
   - Button **Open audit trail**: set `lastRunId` = `pageVars.sel.run_id` → Open page `Audit`.
   - On an error, the flow's error output carries the API `detail`. For example, `422 "a reason is required to reject"`, or `409` when a second approval is still needed. Show it in an alert.

### Page 4: Audit

**Page variable** `a` (`Audit` record). *Page mounted* → **Get record** `Audit` with `run_id` = `appVars.lastRunId` → set `a`.

- **Chain status banner:**
  - Text: `IF(pageVars.a.chain.ok, "✓ Hash chain intact (" + pageVars.a.chain.n_rows + " rows)", "✕ Hash chain BROKEN at seq " + pageVars.a.chain.broken_at + ": " + pageVars.a.chain.reason)`
  - Background: `IF(pageVars.a.chain.ok, "#0ca30c", "#d03b3b")`
- **Hop timeline:** list over `pageVars.a.events`.

  | Field | Binding |
  |---|---|
  | Left | `"#" + repeated.current.seq` |
  | Agent | `IF(repeated.current.agent == "GATE", "Human decision", repeated.current.agent)` |
  | Line 1 | `repeated.current.ts + " · in " + SUBSTRING(repeated.current.input_hash, 0, 10) + " → out " + SUBSTRING(repeated.current.output_hash, 0, 10)` |
  | Line 2 | `"prev " + SUBSTRING(repeated.current.prev_hash, 0, 10) + " → row " + SUBSTRING(repeated.current.event_hash, 0, 10) + " · " + COUNT(repeated.current.data_sources) + " source(s)"` |

- **Report link.** A button **Open NABH evidence report**: **Open URL** `https://<route>/audit/` + `appVars.lastRunId` + `/report`.

  The report route also needs the key, and a plain browser tab can't send the header. So in Build Apps, route the link through the destination with a *Get record* on a data entity `Report` (GET `/audit/{run_id}/report`, response type text), and show its HTML in a **WebView** component. The simpler alternative is to open `https://<route>/ui#/audit/<run_id>` in the fallback UI, which has the report button.

### Page 5: Recall trace

**Page variables:** `alertId` (text) and `r` (`Recall` record).

- An input bound to `alertId`, and a **Trace** button → **Get record** `Recall` with `alert_id` = `pageVars.alertId` → set `r`. On a 404, show the alert "No NSQ alert with that id".
- **Alert card:**
  - `pageVars.r.alert.DRUG + " · batch " + pageVars.r.alert.BATCH`
  - `pageVars.r.alert.REASON`
  - The source badge `pageVars.r.alert.IS_PROXY`
- **Matches:** list over `pageVars.r.matches` with:
  - `repeated.current.PLANT_NAME + " / " + repeated.current.LGORT`
  - `"Batch " + repeated.current.CHARG`
  - `"Unrestricted " + repeated.current.CLABS + " · QI " + repeated.current.CINSM + " · blocked " + repeated.current.CSPEM`
  - `"Expiry " + SUBSTRING(repeated.current.VFDAT, 0, 10)`
  - A match-level chip: `repeated.current.MATCH_LEVEL`
- Footer text: `pageVars.r.note`.

---

## 5. "Shock +30%" button

Put it on the Watchlist top bar with the label `Shock +30%` and the tooltip "What-if: API import cost +30%".

Logic:
1. **Create record** `ShockWhatIf` with body `{"multiplier": 1.3}`. This is fast (~7 s for 38 formulations) and writes nothing.
2. Set page variable `shock` = output. Show a dialog or panel:
   - Headline: `COUNT(SELECT(pageVars.shock.rows, item.band_changed)) + " formulations change flag; " + COUNT(SELECT(pageVars.shock.rows, item.shocked.band == "RED")) + " move to RED"`
   - A list of the first 15 rows, showing `form_id`, `baseline.band → shocked.band` and the headroom before and after.
3. A button **Run the agents on flagged formulations**. It does **Create record** `Run` with body:
   ```
   {"form_ids": SLICE(MAP(SELECT(pageVars.shock.rows, item.shocked.band == "RED"), item.form_id), 0, 10), "shock": 1.3}
   ```
   It takes up to ~1–2 min, so show a spinner. Then set `lastRunId` = `outputs.run_id` and re-run the Watchlist *Get record collection*.
4. Say it on stage exactly as: "Under a 30% API import-cost shock, these formulations' margin headroom falls below threshold. The exit-risk flag is elevated and goes to review."

---

## 6. Demo checklist (plan §10)

1. Watchlist loads, sorted by exposure, with REAL/PROXY/SYNTH badges visible.
2. **Shock +30%** shows the band changes; run the agents; the watchlist refreshes.
3. Open a RED formulation. The chart shows cost approaching the realisation line, plus the dashed shocked cost. Point out "at least N producers" and the top API origin.
4. Scenarios: point at the rejected alternate (shared API origin) and the chosen buffer.
5. Approval: the A5 table, tick the six items, Approve. The PR number and payload appear.
6. Audit: chain intact, every hop listed with hashes, then the report.
7. Recall: enter the alert id to see the batch locations.

If Build Apps or the Wi-Fi fails, open `http://127.0.0.1:8000/ui` on the laptop. It's the same flow against the local API (`docs/deploy.md` §1).
