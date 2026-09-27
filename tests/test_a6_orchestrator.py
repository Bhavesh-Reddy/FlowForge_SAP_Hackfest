import json
from datetime import date

import pytest

from agents import a6_audit as a6
from agents import orchestrator as orch
from agents import report
from agents.contracts import DraftPR
from agents.common import load_fixture_db
AS_OF = date(2026, 9, 26)
RUN = "t-s08"
REC = f"{RUN}:F001:01"
ALL_TICKED = {k: True for k in ("cp01_material_master", "cp02_rol_roq_stock", "cp04_supplier_verified",
                                "price_within_ceiling_gst", "shelf_life_fefo_ok", "cause_reviewed_not_claim")}


@pytest.fixture
def db():
    d = load_fixture_db()  # S01 schema + views, SYNTH fixtures
    yield d
    d.close()


def _rec(db, rec_id=REC, overall="READY", second=False, qty=500.0, price="2.0"):
    run_id, scen = a6.split_rec_id(rec_id)
    draft = DraftPR(material="MAT-0001", plant="H001", quantity=qty, delivery_date=date(2026, 10, 10),
                    purchasing_group="P01", fixed_supplier="M01", unit_price_inr=price)
    db.execute("INSERT INTO FF_AG_RECOMMENDATION (RUN_ID, SCENARIO_ID, FORM_ID, OVERALL, NEEDS_SECOND_APPROVER, "
               "DRAFT_PR_JSON, CREATED_AT) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (run_id, scen, "F001", overall, int(second), json.dumps(draft.to_s4_payload()), "2026-09-26T00:00:00"))
    for agent in ("A1", "A2", "A3", "A4", "A5"):
        a6.hop(db, run_id, agent, {"agent": agent}, {"out": agent}, a6.data_sources(db, a6.AGENT_SOURCES[agent]))


def _decide(db, rules, decision="APPROVE", **kw):
    kw.setdefault("checklist", ALL_TICKED)
    return orch.decide(kw.pop("rec", REC), kw.pop("user", "pharm.lead"), decision, db=db, rules=rules,
                       check_master=False, **kw)


# ---------------------------------------------------------------- chain


def test_chain_verifies(db):
    for i in range(4):
        a6.hop(db, RUN, f"A{i + 1}", {"i": i}, {"o": i}, a6.data_sources(db, ["FF_REF_FORMULATION"]))
    rows = db.query("SELECT SEQ, PREV_HASH, ROW_HASH FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ", (RUN,))
    assert rows["SEQ"].tolist() == [0, 1, 2, 3] and a6._nz(rows.iloc[0]["PREV_HASH"]) is None
    assert rows.iloc[2]["PREV_HASH"] == rows.iloc[1]["ROW_HASH"]
    chk = a6.verify_chain(RUN, db)
    assert chk.ok and chk.n_rows == 4


def test_tampering_one_row_is_detected(db):
    for i in range(4):
        a6.hop(db, RUN, f"A{i + 1}", {"i": i}, {"o": i})
    db.execute("UPDATE FF_AG_AUDIT_LOG SET OUTPUT_HASH = ? WHERE RUN_ID = ? AND SEQ = 2", ("0" * 64, RUN))
    chk = a6.verify_chain(RUN, db)
    assert not chk.ok and chk.broken_at == 2 and "tampered" in chk.reason


def test_deleted_row_is_detected(db):
    for i in range(3):
        a6.hop(db, RUN, "A1", {"i": i}, {"o": i})
    db.execute("DELETE FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? AND SEQ = 1", (RUN,))
    chk = a6.verify_chain(RUN, db)
    assert not chk.ok and chk.broken_at == 1


def test_rule_versions_hash_the_yaml(rules):
    v = a6.rule_file_versions(rules)
    assert set(v) >= {"dpco", "sop_controls", "weights"}
    assert all(s.split("+sha256:")[1].__len__() == 64 for s in v.values())


# ---------------------------------------------------------------- act / gate


def test_act_without_approval_is_refused(db, rules):
    _rec(db)
    with pytest.raises(a6.ActRefused, match="approve"):
        a6.act(REC, db, rules, check_master=False)
    assert db.query("SELECT COUNT(*) AS N FROM FF_MM_EBAN").iloc[0]["N"] == 0


def test_reject_without_reason_is_refused(db, rules):
    _rec(db)
    with pytest.raises(orch.DecisionRefused, match="reason"):
        _decide(db, rules, "REJECT", reason="  ")
    assert db.query("SELECT COUNT(*) AS N FROM FF_AG_APPROVAL").iloc[0]["N"] == 0
    res = _decide(db, rules, "REJECT", reason="alternate supplier already contracted")
    assert res.status == "REJECTED"
    with pytest.raises(a6.ActRefused):
        a6.act(REC, db, rules, check_master=False)


def test_incomplete_checklist_is_refused(db, rules):
    _rec(db)
    with pytest.raises(orch.DecisionRefused, match="checklist"):
        _decide(db, rules, checklist={**ALL_TICKED, "cp04_supplier_verified": False})


def test_approve_posts_s4_shaped_pr(db, rules):
    _rec(db)
    res = _decide(db, rules)
    assert res.status == "ACTIONED" and res.action.banfn == "FF00000001"
    p = res.action.payload
    assert p["PurchaseRequisitionType"] == "NB" and p["PurchaseRequisition"] == "FF00000001"
    item = p["to_PurchaseReqnItem"]["results"][0]
    for k in ("Material", "Plant", "RequestedQuantity", "BaseUnit", "DeliveryDate", "PurchasingGroup", "FixedSupplier"):
        assert k in item
    assert item["RequestedQuantity"] == "500" and item["DeliveryDate"] == "2026-10-10T00:00:00"
    eban = db.query("SELECT MATNR, WERKS, MENGE, FLIEF, AFNAM, RUN_ID FROM FF_MM_EBAN")
    assert eban.to_dict(orient="records") == [{"MATNR": "MAT-0001", "WERKS": "H001", "MENGE": 500, "FLIEF": "M01",
                                               "AFNAM": "pharm.lead", "RUN_ID": RUN}]
    stored = a6.load_action(db, REC)
    assert json.loads(stored["PAYLOAD_JSON"]) == p
    assert a6.verify_chain(RUN, db).ok  # GATE and A6 hops extend the same chain
    with pytest.raises(orch.DecisionRefused, match="already"):
        _decide(db, rules)


def test_edit_approve_changes_qty(db, rules):
    _rec(db)
    res = _decide(db, rules, edits={"qty": 250})
    assert res.decision.action.value == "EDIT_APPROVE"
    assert res.action.payload["to_PurchaseReqnItem"]["results"][0]["RequestedQuantity"] == "250"


def test_second_approver_required_and_distinct(db, rules):
    _rec(db, second=True)
    assert _decide(db, rules).status == "AWAITING_SECOND_APPROVER"
    with pytest.raises(a6.ActRefused, match="second"):
        a6.act(REC, db, rules, check_master=False)
    with pytest.raises(orch.DecisionRefused, match="different"):
        _decide(db, rules, level=2)
    assert _decide(db, rules, user="director.proc", level=2).status == "ACTIONED"


def test_edit_above_threshold_needs_second_approver(db, rules):
    thr = float(rules.param("BUDGET", "second_approver_threshold_inr"))
    _rec(db, price="2.0")
    assert _decide(db, rules, edits={"qty": thr}).status == "AWAITING_SECOND_APPROVER"  # 2 × thr > thr


def test_act_refused_on_broken_chain(db, rules):
    _rec(db)
    db.execute("UPDATE FF_AG_AUDIT_LOG SET AGENT = 'X' WHERE RUN_ID = ? AND SEQ = 0", (RUN,))
    with pytest.raises(a6.ActRefused, match="BROKEN"):
        _decide(db, rules)


def test_master_data_without_key_warns():
    out = a6.check_master_data("MAT-0001", "M01", api_key="")
    assert [o["status"] for o in out] == ["WARN", "WARN"] and "SAP_API_HUB_KEY" in out[0]["note"]


# ---------------------------------------------------------------- orchestrator + report


def test_orchestrator_dry_run_writes_nothing(db, rules):
    r = orch.run(["F001"], 1.3, db=db, rules=rules, dry_run=True, as_of=AS_OF)
    assert [e.agent for e in r.events] == ["A1", "A2", "A3", "A4", "A5"] and r.chain.ok
    assert r.status in ("AWAITING_APPROVAL", "NO_ACTION")
    assert db.query("SELECT COUNT(*) AS N FROM FF_AG_RUN").iloc[0]["N"] == 0
    assert db.query("SELECT COUNT(*) AS N FROM FF_AG_AUDIT_LOG").iloc[0]["N"] == 0


def test_orchestrator_run_stops_at_gate(db, rules):
    r = orch.run(["Amoxicillin"], 1.3, db=db, rules=rules, as_of=AS_OF)
    assert {s.formulation_id for s in r.signals} == {"F001", "F002"}
    assert a6.verify_chain(r.run_id, db).ok
    run_row = db.query("SELECT STATUS, TRIGGER_TYPE FROM FF_AG_RUN WHERE RUN_ID = ?", (r.run_id,)).iloc[0]
    assert run_row["STATUS"] == r.status and run_row["TRIGGER_TYPE"] == "SHOCK"
    assert db.query("SELECT COUNT(*) AS N FROM FF_MM_EBAN").iloc[0]["N"] == 0  # nothing acts before a human


def test_report_has_who_what_why_hashes(db, rules):
    _rec(db)
    _decide(db, rules)
    html = report.render(report.gather(db, REC))
    for s in ("pharm.lead", "FF00000001", "API_PURCHASEREQ_PROCESS_SRV", "chain ok", "probabilistic risk flag"):
        assert s in html
    row_hash = db.query("SELECT ROW_HASH FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? AND SEQ = 0", (RUN,)).iloc[0]["ROW_HASH"]
    assert row_hash in html
