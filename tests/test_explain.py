"""S11: plain-language explanation with a number check and template fallback."""
import json
import time
from datetime import date
from types import SimpleNamespace

import pytest

from agents import explain as ex
from agents.contracts import (
    CauseCode, CheckResult, CheckStatus, DependencyProfile, DraftPR, Forecast, Overall, Recommendation, SignalBand,
)
from agents.ctx import Settings

ANTHROPIC = Settings(llm_provider="anthropic", anthropic_api_key="test-key")
NAME = "Amoxicillin 500 mg"


def _f(value, tag="REAL", unit=None):
    d = {"value": value, "tag": tag, "source": "test"}
    if unit:
        d["unit"] = unit
    return d


@pytest.fixture
def forecast():
    return Forecast(
        run_id="r1", formulation_id="F001", exit_risk_band=SignalBand.RED, exit_risk=0.8, exposure=3.59,
        window_months_lo=0, window_months_hi=6, days_of_cover=22.4, cause_code=CauseCode.CEILING_BELOW_COST,
        cause_facts={
            "headroom_pct": _f(-0.121), "months_to_breach": _f(0.0), "ceiling_price_inr": _f(40.82),
            "realisation_inr": _f(31.99, "PROXY"), "estimated_unit_cost_inr": _f(35.86, "PROXY"),
            "n_producers_min": _f(3), "top_origin_country": _f("CN"), "top_origin_share": _f(0.72),
            "days_of_cover": _f(22.4, "SYNTH"), "lead_time_days": _f(21.0, None),
            "hospital_materials": _f(["MAT-0001"], "SYNTH"), "usable_stock_qty": _f(8000.0, "SYNTH"),
            "daily_consumption": _f(101.3, "SYNTH"),
        })


@pytest.fixture
def dependency():
    return DependencyProfile(run_id="r1", formulation_id="F001", n_producers_min=3, hhi=0.4, top_origin_country="CN",
                             top_origin_share=0.72, affected_materials=["MAT-0001"], affected_wards=["WARD-B"])


@pytest.fixture
def rec():
    return Recommendation(
        run_id="r1", scenario_id="r1:F001:01", overall=Overall.NEEDS_CHANGES, needs_second_approver=True,
        checks=[CheckResult(check_id="PRICE_COMPLIANCE", name="Price compliance", status=CheckStatus.PASS),
                CheckResult(check_id="SUPPLIER_LICENCE", name="Supplier licence", status=CheckStatus.WARN)],
        draft_pr=DraftPR(material="MAT-0001", plant="H001", quantity=8175, delivery_date=date(2026, 10, 25),
                         purchasing_group="P01", fixed_supplier="V100004"))


def _llm(reply, seen=None, delay=0.0):
    def call(sheet, settings, timeout_s):
        if seen is not None:
            seen.append(sheet)
        if delay:
            time.sleep(delay)
        return reply, "claude-opus-5"
    return call


GOOD = ("Amoxicillin 500 mg carries an elevated exit-risk flag for review: estimated unit cost of INR 35.86 "
        "(proxy estimate) is above the estimated realisation of INR 31.99 (proxy estimate), so headroom is -12.1%. "
        "At least 3 producers are on record and 72% of API imports come from CN. "
        "Hospital cover is about 22 days (synthetic data), inside the 0 to 6 month window.")


# ---------------------------------------------------------------- provider none / template


def test_provider_none_uses_template(forecast, dependency, rec):
    out = ex.explain(forecast, dependency, rec, name="Amoxicillin 500 mg", settings=Settings(llm_provider="none"))
    assert not out.used_llm and out.fallback_reason == "LLM_PROVIDER=none"
    assert out.text.startswith("Amoxicillin 500 mg: exit-risk flag RED (a probabilistic risk flag for review)")
    assert "-12.1%" in out.text and "INR 35.86 (proxy)" in out.text and "22 days (synthetic)" in out.text
    assert "at least 3" in out.text.lower() and "second approver" in out.text


def test_template_passes_its_own_checks(forecast, dependency, rec):
    sheet = ex.fact_sheet(forecast, dependency, rec)
    text = ex.template(sheet)
    assert ex.unsupported_numbers(text, sheet) == [] and ex.intent_claims(text) == []


def test_fact_sheet_has_no_hospital_identifiers(forecast, dependency, rec):
    blob = json.dumps(ex.fact_sheet(forecast, dependency, rec, name="Amoxicillin 500 mg"))
    for secret in ("MAT-0001", "H001", "WARD-B", "V100004", "8000", "101.3", "r1:F001", "8175"):
        assert secret not in blob
    assert "-12.1" in blob and '"tag": "PROXY"' in blob  # display-ready values with provenance


# ---------------------------------------------------------------- LLM paths (mocked)


def test_llm_reply_with_made_up_number_falls_back(forecast, dependency, rec, monkeypatch):
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", _llm(GOOD.replace("72%", "85%")))
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC)
    assert not out.used_llm and out.fallback_reason == "number check failed: 85"
    assert out.text == ex.template(ex.fact_sheet(forecast, dependency, rec, NAME))


def test_llm_reply_with_only_fact_numbers_is_used(forecast, dependency, rec, monkeypatch):
    seen = []
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", _llm(GOOD, seen))
    out = ex.explain(forecast, dependency, rec, name="Amoxicillin 500 mg", settings=ANTHROPIC)
    assert out.used_llm and out.text == GOOD and out.fallback_reason is None and out.model == "claude-opus-5"
    assert "MAT-0001" not in json.dumps(seen[0])


def test_spelled_out_number_is_checked_too(forecast, dependency, rec, monkeypatch):
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", _llm(GOOD + " Stock-out is likely within fifteen weeks."))
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC)
    assert not out.used_llm and out.fallback_reason == "number check failed: fifteen"


def test_manufacturer_intent_claim_falls_back(forecast, dependency, rec, monkeypatch):
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", _llm("The maker will discontinue this product; at least 3 producers."))
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC)
    assert not out.used_llm and out.fallback_reason.startswith("intent claim")


def test_timeout_falls_back(forecast, dependency, rec, monkeypatch):
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", _llm(GOOD, delay=1.0))
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC, timeout_s=0.2)
    assert not out.used_llm and out.fallback_reason == "timeout after 0.2s"


def test_provider_error_and_stub_fall_back(forecast, dependency, rec, monkeypatch):
    def boom(*_):
        raise ConnectionError("no network")
    monkeypatch.setitem(ex.PROVIDERS, "anthropic", boom)
    assert ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC).fallback_reason == "anthropic error: ConnectionError"
    out = ex.explain(forecast, dependency, rec, settings=Settings(llm_provider="sap_genai_hub"))
    assert not out.used_llm and "NotImplementedError" in out.fallback_reason


def test_anthropic_request_shape(forecast, dependency, rec, monkeypatch):
    """The real provider function: model default, 8 s / no retries, low effort, refusal fallback, text extraction."""
    calls = {}

    class FakeMessages:
        def create(self, **kw):
            calls["create"] = kw
            return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                   content=[SimpleNamespace(type="thinking", thinking=""),
                                            SimpleNamespace(type="text", text=GOOD)])

    class FakeClient:
        def __init__(self, api_key=None):
            calls["api_key"] = api_key
            self.beta = SimpleNamespace(messages=FakeMessages())

        def with_options(self, **kw):
            calls["options"] = kw
            return self

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC)
    assert out.used_llm and out.text == GOOD
    kw = calls["create"]
    assert kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default" and kw["betas"] == [ex.FALLBACK_BETA]
    assert kw["output_config"] == {"effort": "low"} and "never state or imply" in kw["system"].lower()
    assert calls["options"] == {"timeout": 8.0, "max_retries": 0} and calls["api_key"] == "test-key"
    assert "MAT-0001" not in kw["messages"][0]["content"]


def test_refusal_falls_back(forecast, dependency, rec, monkeypatch):
    class FakeClient:
        def __init__(self, api_key=None):
            self.beta = SimpleNamespace(messages=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(stop_reason="refusal", model=kw["model"], content=[])))

        def with_options(self, **kw):
            return self

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    out = ex.explain(forecast, dependency, rec, name=NAME, settings=ANTHROPIC)
    assert not out.used_llm and out.fallback_reason == "anthropic error: RuntimeError"
