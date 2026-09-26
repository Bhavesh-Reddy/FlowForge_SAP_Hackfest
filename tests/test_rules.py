import copy

import pytest
import yaml

from agents.rules import FILES, RULES_DIR, REQUIRED_CHECKS, RulesError, load_rules, validate


def _doc(name):
    return yaml.safe_load((RULES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def test_all_rules_load(rules):
    assert set(rules.versions) == set(FILES)


def test_section4_constants(rules):
    assert rules.value("weights", "margin.retailer_margin") == 0.16
    assert rules.value("weights", "margin.wholesaler_margin") == 0.10
    assert rules.value("weights", "bands.red_headroom_pct") == 0.05
    assert rules.value("weights", "bands.amber_months_to_breach") == 12
    w = [rules.value("weights", f"concentration.{k}") for k in ("w_inv_producers", "w_hhi", "w_top_origin_share")]
    assert sum(w) == pytest.approx(1.0)
    assert rules.value("weights", "criticality.V") == 1.0
    assert rules.value("weights", "exposure.cover_ratio_max") == 3.0


def test_every_constant_has_source_and_verify(rules):
    c = rules.constant("weights", "margin.retailer_margin")
    assert c["verify"] is True and "para 4" in c["source"]


def test_all_ten_a5_checks_present(rules):
    ids = {c["id"] for c in rules.checks()}
    expected = {cid for v in REQUIRED_CHECKS.values() for cid in v}
    assert len(expected) == 10 and expected <= ids
    assert rules.check("PRICE_COMPLIANCE")["source"].startswith("DPCO 2013 para 14")
    assert rules.param("SHELF_LIFE", "min_residual_months") == 12
    assert rules.param("EXPIRY_WASTE", "max_expired_share") == 0.05


def test_missing_source_rejected():
    doc = _doc("weights")
    del doc["margin"]["retailer_margin"]["source"]
    with pytest.raises(RulesError):
        validate("weights", doc)


def test_verify_must_be_bool():
    doc = _doc("weights")
    doc["bands"]["red_headroom_pct"]["verify"] = "yes"
    with pytest.raises(RulesError):
        validate("weights", doc)


def test_missing_required_constant_rejected():
    doc = _doc("weights")
    del doc["bands"]["amber_headroom_pct"]
    with pytest.raises(RulesError, match="amber_headroom_pct"):
        validate("weights", doc)


def test_missing_check_rejected():
    doc = _doc("sop_controls")
    doc["checks"] = [c for c in doc["checks"] if c["id"] != "BUDGET"]
    with pytest.raises(RulesError, match="BUDGET"):
        validate("sop_controls", doc)


def test_bad_check_param_rejected():
    doc = copy.deepcopy(_doc("sop_controls"))
    doc["checks"][1]["params"]["nsq_lookback_months"] = {"value": 12}
    with pytest.raises(RulesError):
        validate("sop_controls", doc)


def test_load_from_dir(tmp_path):
    for f in FILES:
        (tmp_path / f"{f}.yaml").write_text((RULES_DIR / f"{f}.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    assert load_rules(tmp_path).value("dpco", "constants.gst_rate_medicines") > 0
