# Finale demo script (plan §10): 6 minutes

Two people: the **speaker** talks, the **driver** clicks. The driver uses the Build Apps app (primary) or the
fallback UI at `<API>/ui` (same pages, same API: Watchlist · Molecule · Approval · Audit · Recall).
Say numbers **as they appear on screen**. Do not memorise them: they change with every reset.

**Which API to click through:** the **CF app** (it runs in BTP next to HANA), or the **local API on SQLite**.
Never a laptop API talking to HANA on stage: from a laptop every HANA query is ~300 ms, and the S13 check measured
about 6 minutes for the click-through steps that way.

**Before walking on:** `docs/preflight.md` is done. In particular, `python scripts/demo_reset.py --backend hana --prepare`
ran this morning, so the watchlist and detail pages already show the saved **+30% shock run** and nothing is approved yet.

**Words we never say:** "Joule-powered", "predicts that X will discontinue", "real S/4 posting",
"validated model". Every flag is *a probabilistic flag for review*. Cost lines are *estimated; synthetic in this demo*.

## Timeline

| # | Time | Driver clicks | Speaker says (short version) | If it fails |
|---|---|---|---|---|
| 1 | 0:00–0:30 **Hook** | Slide: pharmacy shelf photo (PSG visit) | "The first sign of a shortage isn't an empty shelf. It's a cost rising towards a price that isn't allowed to rise." | Keep talking; the slide is not needed |
| 2 | 0:30–1:15 **Mechanism** | Watchlist → click the pre-picked RED row (default **Digoxin 0.25 mg**) → Molecule detail, chart "Ceiling vs estimated cost" | "This red line is the NPPA ceiling, fixed by DPCO and revised once a year by WPI. Under it is what the manufacturer realises after trade margins. This line is the estimated unit cost: API cost plus conversion. When cost meets realisation, making the medicine stops being viable. Our cost line is synthetic for this demo; in production it comes from TradeStat import unit values." | Show the same chart as a screenshot slide |
| 3 | 1:15–3:15 **Live run** | Top bar **Shock +30%** → the table shows margin flag before/after → back to Watchlist (RED rows) → open the same RED row | "A tariff or freight shock pushes API import cost up 30%. A1 re-scores every tracked medicine: *N* move to RED." Then on the detail page: "A2: at least *N* producers on record, *X*% of the API from one country. A3: exit-risk window *a–b* months, hospital cover *d* days, stated cause *…*." Point at the REAL / PROXY / SYNTH labels. | Shock fails → read the saved shocked run on the Watchlist (it is already there from `--prepare`) |
| 4 | 3:15–3:45 **Fake-fix catch** | Molecule detail → "Scenarios (A4) and checks (A5)" → point at the row marked **Correlated risk** (rank "—") | "The cheapest alternative was rejected: it shares the same API origin as the current supply, so it would fail together. The next option is sized so nothing expires before use." | Say it from the reason text shown on the row |
| 5 | 3:45–4:30 **Human gate** | Approval → **Open approval queue** → the same medicine's **READY** recommendation → show the A5 table → tick all 6 items in "Manual verification" → Approver `chief.pharmacist` → **Approve**. If it asks for level 2: Approver `director` → **Approve (level 2)** | "Nothing is bought by the system. The Chief Pharmacist checks material, stock level, supplier, price within ceiling plus GST, shelf life, and that this is a risk flag, not a claim about the manufacturer." | If the queue is empty, someone approved already: reset with `--prepare` before the next run-through (never on stage) |
| 6 | 4:30–5:00 **Action + audit** | The response shows the PR number (`FF…`) and payload → **Open audit trail** → chain **ok**, hops A1 … A5, GATE, A6 → **Open NABH evidence report** | "Only now does A6 raise a purchase requisition in the SAP S/4HANA API shape. It is a mock posting into our PR table, not a real S/4 system. Every hop is hash-chained with its sources: that's the NABH evidence." | Show the audit trail only; the report is optional |
| 7 | 5:00–5:20 **Recall** | Recall → alert id `NSQ-3F40E3F8B095` → **Load** | "A real CDSCO Not-of-Standard-Quality alert: gentamicin batch 23BUH50. One click finds it in four locations: central pharmacy and A, B, C Blocks." | Say it from the slide screenshot |
| 8 | 5:20–6:00 **Proof and close** | Slide: backtest (`docs/backtest_results.md`, `docs/img/backtest_leadtime.png`) | "We replay the agents month by month on NPPA's own Para-19 price rescues, with no look-ahead. Today it's a pipeline check: two confirmed events and synthetic costs, so we don't claim accuracy yet. With real TradeStat costs this becomes our evidence. The design scales from 40 to 900 medicines, sold per hospital group." | Skip to the close |

## Fallbacks (decide in 10 seconds, don't debug on stage)

| Problem | Switch to | How |
|---|---|---|
| HANA down or slow | **SQLite on the laptop** | `python scripts/demo_reset.py --backend sqlite --prepare` (under 1 min), then start the local API (below) and point the UI at it |
| CF app down or asleep | **Local API on SQLite** | `$env:DB_BACKEND="sqlite"`, `$env:APP_API_KEY="…"`, `uvicorn api.main:app --port 8000` (a local API on HANA is too slow for live clicks) |
| Build Apps down | **Fallback UI** | open `<API>/ui` (CF route or `http://127.0.0.1:8000/ui`), enter the API URL and key in the top bar, **Connect** |
| No network at all | **Offline** | local API on SQLite with `$env:LLM_PROVIDER="none"` (explanations use the checked template); phone hotspot if you only need GitHub/slides |
| Anything else | **Backup video** | play the recorded run-through from the desktop |

## Rehearsal check (before every run-through)

```powershell
python scripts/demo_reset.py --backend sqlite --check     # every step must say PASS
python scripts/demo_reset.py --backend sqlite --prepare   # leave the demo state clean again
```

`--check` approves a recommendation and posts a PR, so always follow it with `--prepare` (or a plain reset) before
presenting. Two clean runs in a row = ready (plan §9, D-1 exit criterion).
