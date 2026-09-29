# Finale-morning preflight (plan §9 D0, §11)

Tick every box before the slot. Owner in brackets. Never paste the HANA password, host or API key into chat or slides.

## T − 90 min: data and backend

- [ ] **HANA is running** [B1]. HANA Cloud Central → Hackfest-DB → status *Running*. Then `python scripts/day1_checks.py`: connect PASS, create+drop PASS.
- [ ] **Rebuild and prepare the demo data** [B1]: `python scripts/demo_reset.py --backend hana --prepare` (about 2 min; budget 5).
      Every line PASS. It resets only FF_ objects in our schema (other teams share the password; this undoes any damage).
      How it works on HANA: the seeds are loaded into HANA; the agents run on a local SQLite mirror of the same seeds
      (from a laptop each HANA query is ~300 ms and the shared instance drops long sessions, so a run over the network
      does not finish), and their FF_AG_* results and audit chain are published to HANA. Same code, same rules, same data.
- [ ] **Row counts** [B1]: `python scripts/day1_checks.py` → about 36,500 rows in the FF_ tables; FF_REF_FORMULATION 905,
      FF_REF_NSQ_ALERT 3,445, FF_MM_MCHB 192, FF_AG_RUN 2 (baseline + shocked run).
- [ ] **Offline copy ready** [B1]: `python scripts/demo_reset.py --backend sqlite --prepare` (under 1 min; pause OneDrive
      sync first if the repo is inside OneDrive, it slows SQLite writes).

## T − 60 min: API and screens

- [ ] **CF app awake** [B3]: `cf app flowforge-api` shows *running*; open `https://<route>/health` → `{"status":"ok","db_backend":"hana"}`.
      A trial app may have been stopped overnight: `cf start flowforge-api`.
- [ ] **API key works** [B3]: `curl -H "X-API-Key: <key>" https://<route>/watchlist?limit=3` returns rows. Keep the key on paper, not on screen.
- [ ] **Build Apps** [B3]: app opens, Watchlist loads through the `FLOWFORGE_API` destination, REAL / PROXY / SYNTH badges visible.
- [ ] **Fallback UI** [B3]: `https://<route>/ui` → Connect with URL + key → Watchlist loads. Also start the local API once
      on SQLite (`$env:DB_BACKEND="sqlite"; uvicorn api.main:app --port 8000`) and check `http://127.0.0.1:8000/ui`.
      Do not click through a laptop API that talks to HANA: each query is a ~300 ms round trip.
- [ ] **LLM setting** [B3]: `LLM_PROVIDER=none` unless the venue network is proven; the template explanation is checked and safe.
- [ ] **Full dry run** [all]: follow `docs/demo_script.md` once end to end, then `python scripts/demo_reset.py --backend hana --prepare` again
      (the dry run approves a recommendation; the stage run must start clean).

## T − 30 min: room

- [ ] **Projector** [P2]: duplicate display at 1920×1080 (fall back to 1280×720); browser zoom 110–125% so tables are readable from the back;
      light theme on a bright projector (◐ button in the UI).
- [ ] **Network** [P2]: venue Wi-Fi tested with `/health`; **phone hotspot** on, tested, and the laptop already knows it.
- [ ] **Laptop** [P2]: charger plugged in; notifications, Windows Update and OneDrive sync paused; sleep disabled; only the demo tabs open
      (slides, Build Apps, fallback UI, audit report).
- [ ] **Backup video** [P2]: on the desktop, plays offline, sound checked.
- [ ] **Recall id on a card** [P2]: `NSQ-3F40E3F8B095` (gentamicin batch 23BUH50).
- [ ] **Words** [P1]: "probabilistic flag for review", "estimated cost (synthetic in this demo)", "mock S/4 posting". Never "Joule", never "will discontinue".

## If something is red

| Red item | Do this |
|---|---|
| HANA not running / `demo_reset --backend hana` fails | Present on SQLite: `demo_reset --backend sqlite --prepare` + local API + fallback UI (`docs/demo_script.md` fallbacks) |
| CF app won't start | Local API against HANA (`$env:DB_BACKEND="hana"`) or SQLite |
| Build Apps won't load | Fallback UI on the same API |
| No network | Local API on SQLite, `LLM_PROVIDER=none` |
