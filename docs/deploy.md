# Deploying the FlowForge API (S09)

The API is FastAPI (`api/main.py`). Locally it runs with uvicorn. On SAP BTP it runs on Cloud Foundry with the Python buildpack (`manifest.yml`, `runtime.txt`, `Procfile`). HANA credentials and the API key live in a user-provided service called `ff-hana`. They never go in the repo, the manifest or a command line.

Every route except `/health` needs the header `X-API-Key: <APP_API_KEY>`.

## 1. Run locally

```powershell
# one-time: build data/flowforge.sqlite (gitignored) from data/seed + the SYNTH fixtures
python -m ingest.load_hana --backend sqlite --reset --seed

# pick any local key; set env vars for this shell only
$env:DB_BACKEND = "sqlite"
$env:APP_API_KEY = "local-dev-key"
uvicorn api.main:app --port 8000
```

Then:
- http://127.0.0.1:8000/health returns `{"status":"ok",...}`. No key is needed.
- http://127.0.0.1:8000/docs opens Swagger. Click **Authorize**, paste the key, and try `GET /watchlist`.
- From a terminal: `curl -H "X-API-Key: local-dev-key" http://127.0.0.1:8000/watchlist?limit=5`

The watchlist lists every formulation. Run the agents to fill in the risk columns: `POST /run {"form_ids": [], "shock": 1.3}` scores the tracked set, which is every formulation with a BOM assumption.

To run against the shared HANA instead, set `DB_BACKEND=hana` and the `HANA_*` variables in your gitignored `.env`.

## 2. Cloud Foundry (BTP trial)

You need the CF CLI v8 (`cf version`) and a BTP trial account with a Cloud Foundry environment and a space (default: `dev`).

### 2.1 Log in

Find your trial region in the BTP cockpit, under Subaccount → Overview → Cloud Foundry Environment → API Endpoint.

```
cf login -a https://api.cf.us10-001.hana.ondemand.com --sso     # US East (VA) trial
cf login -a https://api.cf.ap21.hana.ondemand.com --sso         # Singapore (Azure) trial
```

`--sso` prints a URL. Open it, sign in, and paste the one-time passcode. Then pick your org and the `dev` space.

### 2.2 Create the credentials service (you type the values)

The CF CLI prompts for each value, so nothing lands in your shell history:

```
cf create-user-provided-service ff-hana -p "host, port, user, password, schema, app_api_key"
```

| prompt | value |
|---|---|
| host | HANA Cloud SQL endpoint host, from HANA Cloud Central; no `https://` and no port |
| port | `443` |
| user | the Hackfest DB user (`HACKFEST0xxx`) |
| password | the HANA password |
| schema | normally the same as `user` |
| app_api_key | a long random string that SAP Build Apps will send as `X-API-Key` |

To add the SAP API Business Hub key for the read-only master-data check, include `sap_api_hub_key` in the `-p` list.

If you mistype a value, run `cf update-user-provided-service ff-hana -p "host, port, user, password, schema, app_api_key"`, then `cf restage flowforge-api`.

### 2.3 Push

From the repo root, which holds `manifest.yml`:

```
cf push
```

`.cfignore` keeps `.env`, `data/`, `*.sqlite`, `.git` and docs out of the upload. The manifest binds `ff-hana`. At start-up, `api/cf.py` copies its credentials into `HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`, `HANA_SCHEMA`, `APP_API_KEY` and `SAP_API_HUB_KEY`, but only where those variables are not already set. The health check is `GET /health`, which doesn't touch the database. That way a HANA outage doesn't put the app into a restart loop.

The first push takes a few minutes because pandas, OR-Tools and statsmodels are large.

### 2.4 Test from a browser

`cf app flowforge-api` shows the route, for example `flowforge-api-xyz.cfapps.us10-001.hana.ondemand.com`.

- `https://<route>/health` returns `{"status":"ok","db_backend":"hana",...}`.
- `https://<route>/docs`: click **Authorize**, paste the `app_api_key`, then run `GET /watchlist`. You should get rows with no 500 error; a 500 usually means CF can't reach HANA (see below).
- With curl: `curl -H "X-API-Key: <key>" https://<route>/watchlist?limit=5`

If `/watchlist` fails, run `cf logs flowforge-api --recent`. The usual cause is that HANA Cloud doesn't allow the CF egress IPs. In HANA Cloud Central, open the instance's **Connections** setting. It must allow all IP addresses, or include your CF region's egress range. This is the "HANA from CF" Day-1 check in STATUS.

### 2.5 SAP Build Apps

- Create a BTP destination, or call the route directly, with the header `X-API-Key`.
- CORS already allows `*.hana.ondemand.com`, `*.build.cloud.sap`, `*.cloud.sap` and the AppGyver preview domains, plus `localhost` for the fallback UI (S10).
- To allow more origins: `cf set-env flowforge-api CORS_ORIGINS "https://a.example,https://b.example"`, then `cf restage flowforge-api`.

## 3. Routes

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | public; no DB access |
| GET | `/watchlist?limit=100&assessed_only=false` | `FF_V_WATCHLIST`, highest exit risk first |
| GET | `/molecule/{form_id}` | latest signal, dependency, forecast, scenarios, checks, graph nodes/edges, data tags |
| POST | `/run` | `{form_ids, shock, dry_run}`; A1→A5 stop at the human gate; at most 50 formulations |
| POST | `/scenario/shock` | `{multiplier, form_ids}`; A1-only what-if, writes nothing |
| GET | `/approvals/pending` | gate items from each formulation's latest run, with the level they wait for |
| POST | `/approval` | `{rec_id, decision, checklist, edits, reason, user, level, snooze_days}`; approving runs A6 act (the PR goes to the `FF_MM_EBAN` mock) |
| GET | `/audit/{run_id}` | audit events and hash-chain status |
| GET | `/audit/{run_id}/report?rec_id=` | NABH evidence HTML |
| GET | `/recall/{alert_id}` | NSQ alert → matching hospital batches |
