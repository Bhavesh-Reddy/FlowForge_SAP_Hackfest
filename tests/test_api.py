"""S09 API on SQLite with the SYNTH fixtures (httpx TestClient)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.common import FIXTURE_CEILING_FROM
from agents.ctx import Settings, get_db
from api import cf
from api.main import create_app
from ingest import load_hana as lh

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "test-key"
H = {"X-API-Key": KEY}
ALL_TICKED = {k: True for k in ("cp01_material_master", "cp02_rol_roq_stock", "cp04_supplier_verified",
                                "price_within_ceiling_gst", "shelf_life_fefo_ok", "cause_reviewed_not_claim")}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("api") / "api.sqlite"
    with get_db(Settings(db_backend="sqlite"), sqlite_path=path) as d:
        lh.create_objects(d)
        lh.seed(d, [FIXTURES])
        d.execute("UPDATE FF_REF_CEILING_PRICE SET EFFECTIVE_FROM = ? WHERE SOURCE = ?",
                  (FIXTURE_CEILING_FROM, "FIXTURE_S00"))
        d.execute("INSERT INTO FF_CFG_PARAM (NAME, DATE_VALUE) VALUES ('AS_OF_DATE', '2026-09-26')")
        lh.upsert_rows(d, lh.schema_tables()["FF_REF_NSQ_ALERT"], [{
            "ALERT_ID": "NSQ-T1", "MONTH": "2026-08-01", "DRUG": "Amoxicillin Capsules", "BATCH": "B2601",
            "REASON": "Dissolution", "SOURCE": "test", "IS_PROXY": "SYNTH"}])
    return path


def _db(path):
    return lambda: get_db(Settings(db_backend="sqlite"), sqlite_path=path)


@pytest.fixture(scope="module")
def client(db_path):
    app = create_app(Settings(db_backend="sqlite", app_api_key=KEY), _db(db_path))
    return TestClient(app)


@pytest.fixture(scope="module")
def ran(client):
    r = client.post("/run", json={"form_ids": ["F001"], "shock": 1.3}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _one(db_path, sql, params=()):
    with get_db(Settings(db_backend="sqlite"), sqlite_path=db_path) as d:
        return d.query(sql, params)


# ---------------------------------------------------------------- auth


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/watchlist", "/molecule/F001", "/approvals/pending", "/audit/x", "/recall/x"])
def test_auth_required(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/run", json={"form_ids": ["F001"]}).status_code == 401


def test_no_key_configured_refuses_everything(db_path):
    c = TestClient(create_app(Settings(db_backend="sqlite", app_api_key=""), _db(db_path)))
    assert c.get("/watchlist", headers={"X-API-Key": ""}).status_code == 503
    assert c.get("/health").status_code == 200


def test_cors_allows_build_apps_origin(client):
    r = client.options("/watchlist", headers={"Origin": "https://myapp.eu10.build.cloud.sap",
                                              "Access-Control-Request-Method": "GET",
                                              "Access-Control-Request-Headers": "X-API-Key"})
    assert r.headers.get("access-control-allow-origin") == "https://myapp.eu10.build.cloud.sap"
    r = client.options("/watchlist", headers={"Origin": "https://evil.example.com",
                                              "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------- read routes


def test_watchlist_returns_rows(client, ran):
    r = client.get("/watchlist", headers=H)
    assert r.status_code == 200
    rows = r.json()
    assert rows and {"F001", "F002"} <= {x["form_id"] for x in rows}
    top = rows[0]
    assert top["form_id"] == "F001" and top["run_id"] == ran["run_id"]  # only assessed row sorts first
    assert top["producers_label"].startswith("at least ") and top["data_tag"] == "SYNTH"


def test_molecule_detail(client, ran):
    r = client.get("/molecule/F001", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["run_id"] == ran["run_id"] and d["signal"]["formulation_id"] == "F001"
    assert d["dependency"]["n_producers_min"] >= 1 and d["forecast"]["cause_code"]
    assert d["scenarios"] and d["recommendations"][0]["recommendation"]["checks"]
    assert d["graph"]["nodes"] and d["graph"]["edges"] and "FORMULATION:F001" in {n["id"] for n in d["graph"]["nodes"]}
    assert d["data_tags"]["formulation"] == "SYNTH"
    assert client.get("/molecule/NOPE", headers=H).status_code == 404


def test_shock_what_if_writes_nothing(client, db_path):
    before = _one(db_path, "SELECT COUNT(*) AS N FROM FF_AG_SIGNAL").iloc[0]["N"]
    r = client.post("/scenario/shock", json={"multiplier": 1.3, "form_ids": ["F001"]}, headers=H)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["shocked"]["headroom_pct"] < row["baseline"]["headroom_pct"]
    assert _one(db_path, "SELECT COUNT(*) AS N FROM FF_AG_SIGNAL").iloc[0]["N"] == before


def test_recall_trace(client):
    r = client.get("/recall/NSQ-T1", headers=H)
    assert r.status_code == 200 and r.json()["matches"][0]["CHARG"] == "B2601"
    assert client.get("/recall/NONE", headers=H).status_code == 404


# ---------------------------------------------------------------- gate


def test_reject_without_reason_is_422(client, ran):
    rec = ran["awaiting_approval"][0]
    r = client.post("/approval", json={"rec_id": rec, "decision": "REJECT", "user": "pharm.lead"}, headers=H)
    assert r.status_code == 422 and "reason" in r.json()["detail"]


def test_approve_creates_pr(client, ran, db_path):
    rec = ran["awaiting_approval"][0]
    pending = client.get("/approvals/pending", headers=H).json()
    assert rec in {p["rec_id"] for p in pending}
    r = client.post("/approval", json={"rec_id": rec, "decision": "APPROVE", "user": "pharm.lead",
                                       "checklist": ALL_TICKED}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIONED" and body["banfn"].startswith("FF")
    item = body["payload"]["to_PurchaseReqnItem"]["results"][0]
    assert item["Material"] and item["Plant"] and item["RequestedQuantity"]
    eban = _one(db_path, "SELECT BANFN, SCENARIO_ID FROM FF_MM_EBAN")
    assert eban.to_dict(orient="records") == [{"BANFN": body["banfn"], "SCENARIO_ID": rec}]
    assert rec not in {p["rec_id"] for p in client.get("/approvals/pending", headers=H).json()}
    again = client.post("/approval", json={"rec_id": rec, "decision": "APPROVE", "user": "pharm.lead",
                                           "checklist": ALL_TICKED}, headers=H)
    assert again.status_code == 422 and "already" in again.json()["detail"]


def test_audit_and_report(client, ran):  # runs after test_approve_creates_pr (file order)
    r = client.get(f"/audit/{ran['run_id']}", headers=H)
    assert r.status_code == 200
    a = r.json()
    assert a["chain"]["ok"] and [e["agent"] for e in a["events"]][:5] == ["A1", "A2", "A3", "A4", "A5"]
    assert {"GATE", "A6"} <= {e["agent"] for e in a["events"]}
    html = client.get(f"/audit/{ran['run_id']}/report", headers=H)
    assert html.status_code == 200 and "text/html" in html.headers["content-type"]
    assert "pharm.lead" in html.text and "chain ok" in html.text
    assert client.get("/audit/unknown-run", headers=H).status_code == 404


# ---------------------------------------------------------------- Cloud Foundry


def test_vcap_fills_env_without_overriding():
    env = {"VCAP_SERVICES": '{"user-provided": [{"name": "ff-hana", "credentials": {"host": "h.example", '
                            '"port": "443", "user": "U1", "password": "p", "app_api_key": "k"}}]}',
           "HANA_USER": "already-set"}
    applied = cf.apply_vcap(env)
    assert env["HANA_HOST"] == "h.example" and env["HANA_USER"] == "already-set" and env["DB_BACKEND"] == "hana"
    assert env["APP_API_KEY"] == "k" and "HANA_USER" not in applied and "p" not in applied
