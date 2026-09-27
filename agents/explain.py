"""Plain-language explanation of a flag for the Chief Pharmacist (plan §2, flowforge skill §5).

The LLM only narrates. It receives a JSON fact sheet built from A3's cause_facts (plus A2/A5 summaries),
never hospital identifiers (material codes, plant, wards, stock quantities). Every number in its reply
must match a fact-sheet value (allowing rounding); otherwise the reply is discarded and the deterministic
template is used. Any error, refusal, missing key or the 8 s timeout also falls back to the template.

LLM_PROVIDER: anthropic (model from LLM_MODEL, default claude-opus-5) | none (template only) |
sap_genai_hub (stub: not implemented, always falls back).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from agents.contracts import CheckStatus, DependencyProfile, Forecast, Recommendation
from agents.ctx import Settings, load_settings

log = logging.getLogger(__name__)

TIMEOUT_S = 8.0
DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 1024  # 3-4 sentences
# Server-side refusal fallback (routes a declined request to Anthropic's recommended model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")

CAUSE_LABEL = {
    "CEILING_BELOW_COST": "the NPPA ceiling is below the estimated cost of making it",
    "HEADROOM_ERODING": "margin headroom under the ceiling is eroding",
    "SINGLE_ORIGIN_API": "the API (active ingredient) comes mostly from a single country",
    "FEW_PRODUCERS": "few producers are on record",
    "QUALITY_NSQ": "recent CDSCO not-of-standard-quality alerts",
    "SUPPLIER_OTD_DROP": "supplier on-time delivery has dropped",
}
TAG_WORD = {"PROXY": "proxy", "SYNTH": "synthetic"}

SYSTEM_PROMPT = """You explain medicine supply-risk flags to a hospital Chief Pharmacist in India.
Write 3 or 4 plain sentences. Use only the facts in the JSON fact sheet you are given.
Rules:
- Use only numbers that appear in the fact sheet, written exactly as given (digits, not words). Do not compute new numbers, totals, differences or dates.
- When a fact's tag is PROXY or SYNTH, say so next to it, e.g. "(proxy estimate)" or "(synthetic data)".
- This is a probabilistic risk flag for review. Never state or imply what a manufacturer intends or will do (no "will exit", "will discontinue", "plans to"). Say "exit risk elevated" or "margin headroom below threshold" instead.
- Producer counts are lower bounds: write "at least N producers".
- No identifiers, codes, headings, bullet points or markdown. Plain sentences only."""


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    used_llm: bool
    fallback_reason: str | None = None
    provider: str = "none"
    model: str | None = None


# ---------------------------------------------------------------- fact sheet


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fv(facts: dict[str, Any], key: str) -> tuple[Any, str | None]:
    f = facts.get(key)
    if isinstance(f, dict):
        return f.get("value"), f.get("tag")
    return f, None


def _r(x: float | None, d: int) -> float | int | None:
    if x is None:
        return None
    y = round(x, d)
    return int(y) if d == 0 else y


def fact_sheet(forecast: Forecast, dependency: DependencyProfile | None = None,
               recommendation: Recommendation | None = None, name: str | None = None) -> dict[str, Any]:
    """Display-ready facts (percentages already x100, money in INR). No hospital identifiers."""
    cf = forecast.cause_facts or {}
    facts: dict[str, dict[str, Any]] = {}

    def add(key: str, value: Any, unit: str | None = None, tag: str | None = None, digits: int | None = None) -> None:
        if value is None or value == "":
            return
        if digits is not None:
            value = _r(_num(value), digits)
            if value is None:
                return
        item: dict[str, Any] = {"value": value}
        if unit:
            item["unit"] = unit
        if tag in ("REAL", "PROXY", "SYNTH"):
            item["tag"] = tag
        facts[key] = item

    h, h_tag = _fv(cf, "headroom_pct")
    add("margin_headroom", _num(h) * 100 if _num(h) is not None else None, "% of manufacturer realisation", h_tag, 1)
    mtb, _ = _fv(cf, "months_to_breach")
    add("months_to_breach", mtb, "months", h_tag, 1)
    for key, out, unit in (("ceiling_price_inr", "ceiling_price", "INR per unit, excl. GST"),
                           ("realisation_inr", "manufacturer_realisation", "INR per unit (estimate)"),
                           ("estimated_unit_cost_inr", "estimated_unit_cost", "INR per unit (estimate)")):
        v, t = _fv(cf, key)
        add(out, v, unit, t, 2)

    n, n_tag = _fv(cf, "n_producers_min")
    if n is None and dependency is not None:
        n = dependency.n_producers_min
    add("producers_at_least", n, None, n_tag, 0)
    country, c_tag = _fv(cf, "top_origin_country")
    share, s_tag = _fv(cf, "top_origin_share")
    if country is None and dependency is not None:
        country, share = dependency.top_origin_country, dependency.top_origin_share
    add("top_api_origin_country", country, None, c_tag)
    add("top_api_origin_share", _num(share) * 100 if _num(share) is not None else None, "% of API imports", s_tag, 0)
    nsq, nsq_tag = _fv(cf, "nsq_alerts_recent")
    if _num(nsq):
        add("recent_nsq_alerts", nsq, None, nsq_tag, 0)
    otd, otd_tag = _fv(cf, "otd_drop_pts")
    if _num(otd):
        add("supplier_on_time_delivery_drop", otd, "percentage points", otd_tag, 1)

    add("exit_risk_window_from", forecast.window_months_lo, "months", None, 0)
    add("exit_risk_window_to", forecast.window_months_hi, "months", None, 0)
    cover, cover_tag = _fv(cf, "days_of_cover")
    add("hospital_days_of_cover", cover if cover is not None else forecast.days_of_cover, "days", cover_tag, 0)
    lt, _ = _fv(cf, "lead_time_days")
    add("supplier_lead_time", lt, "days", None, 0)

    sheet: dict[str, Any] = {
        "formulation": name,
        "exit_risk_flag": forecast.exit_risk_band.value,
        "stated_cause": CAUSE_LABEL.get(forecast.cause_code.value, forecast.cause_code.value),
        "facts": facts,
    }
    if recommendation is not None:
        attention = sorted(c.name for c in recommendation.checks if c.status is not CheckStatus.PASS)  # stable order
        sheet["recommended_action"] = {
            "a5_verdict": recommendation.overall.value.replace("_", " ").lower(),
            "checks_needing_attention": attention,
            "second_approver_required": recommendation.needs_second_approver,
        }
    return {k: v for k, v in sheet.items() if v is not None}


def allowed_numbers(sheet: dict[str, Any]) -> list[float]:
    """Fact values, plus numbers inside the formulation name (e.g. the strength in "Amoxicillin 500 mg")."""
    out = []
    for f in sheet.get("facts", {}).values():
        v = _num(f.get("value"))
        if v is not None:
            out.append(v)
    out += [x for _, x in extract_numbers(sheet.get("formulation") or "")]
    return out


# ---------------------------------------------------------------- number check

_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])[-−+]?\d[\d,]*(?:\.\d+)?")
_WORDS = {w: i for i, w in enumerate(
    "zero _ two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split()) if w != "_"}  # "one" is too often not a number
_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
               "hundred": 100, "thousand": 1000, "lakh": 100000, "crore": 10000000, "dozen": 12})
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.IGNORECASE)


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Every numeric token in the text, including spelled-out numbers (so they can't slip past the check)."""
    found = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        try:
            found.append((raw, float(raw.replace(",", "").replace("−", "-"))))
        except ValueError:
            continue
    for m in _WORD_RE.finditer(text):
        found.append((m.group(0), float(_WORDS[m.group(0).lower()])))
    return found


def _matches(raw: str, x: float, allowed: list[float]) -> bool:
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    tol = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(abs(x) - abs(v)) <= tol for v in allowed)


_INTENT_RE = re.compile(
    r"\b(will|would|going to|plans? to|planning to|intends? to|intending to|decided to|expected to)\s+"
    r"(exit|discontinue|stop|withdraw|leave|quit|cease|halt|abandon)", re.IGNORECASE)


def intent_claims(text: str) -> list[str]:
    """Phrases that claim what a manufacturer will do (forbidden: flags are probabilistic, for review)."""
    return [m.group(0) for m in _INTENT_RE.finditer(text)]


def unsupported_numbers(text: str, sheet: dict[str, Any]) -> list[str]:
    allowed = allowed_numbers(sheet)
    return [raw for raw, x in extract_numbers(text) if not _matches(raw, x, allowed)]


# ---------------------------------------------------------------- template


def _uniform_tag(facts: dict[str, Any]) -> str | None:
    """The single PROXY/SYNTH tag shared by every tagged fact, if there is exactly one kind."""
    tags = {f.get("tag") for f in facts.values() if f.get("tag")}
    return tags.pop() if len(tags) == 1 and tags <= {"PROXY", "SYNTH"} else None


def template(sheet: dict[str, Any]) -> str:
    """Deterministic explanation; every number comes straight from the fact sheet."""
    f = sheet.get("facts", {})
    uniform = _uniform_tag(f)

    def _fmt(item: dict[str, Any] | None, fmt: str) -> str | None:
        if not item:
            return None
        s = fmt.format(item["value"])
        word = None if uniform else TAG_WORD.get(item.get("tag") or "")
        return f"{s} ({word})" if word else s

    who = sheet.get("formulation") or "This formulation"
    parts = [f"{who}: exit-risk flag {sheet['exit_risk_flag']} (a probabilistic risk flag for review); "
             f"stated cause: {sheet['stated_cause']}."]
    cost, real, ceil = (_fmt(f.get("estimated_unit_cost"), "INR {}"), _fmt(f.get("manufacturer_realisation"), "INR {}"),
                        _fmt(f.get("ceiling_price"), "INR {}"))
    head = _fmt(f.get("margin_headroom"), "{}%")
    if cost and real:
        s = f"Estimated unit cost is {cost} against an estimated manufacturer realisation of {real}"
        s += f" under the NPPA ceiling of {ceil} excl. GST" if ceil else ""
        s += f", so margin headroom is {head}." if head else "."
        parts.append(s)
    supply = []
    if f.get("producers_at_least"):
        supply.append(f"at least {_fmt(f['producers_at_least'], '{}')} producers are on record")
    if f.get("top_api_origin_country") and f.get("top_api_origin_share"):
        supply.append(f"{_fmt(f['top_api_origin_share'], '{}%')} of API imports come from "
                      f"{_fmt(f['top_api_origin_country'], '{}')}")
    if f.get("hospital_days_of_cover"):
        supply.append(f"hospital cover is about {_fmt(f['hospital_days_of_cover'], '{} days')}")
    if f.get("exit_risk_window_to") is not None and f.get("exit_risk_window_from") is not None:
        supply.append(f"the exit-risk window is {f['exit_risk_window_from']['value']} to "
                      f"{f['exit_risk_window_to']['value']} months")
    if supply:
        parts.append(supply[0][0].upper() + "; ".join(supply)[1:] + ".")
    ra = sheet.get("recommended_action")
    if ra:
        s = f"The recommended action is {ra['a5_verdict']} after the policy checks"
        if ra["checks_needing_attention"]:
            s += " (review: " + ", ".join(ra["checks_needing_attention"]) + ")"
        s += "; a second approver is required." if ra["second_approver_required"] else "."
        parts.append(s)
    if uniform:
        parts.append(f"All tagged figures are {'proxy estimates' if uniform == 'PROXY' else 'synthetic data'}.")
    return " ".join(parts)


# ---------------------------------------------------------------- providers


def _call_anthropic(sheet: dict[str, Any], settings: Settings, timeout_s: float) -> tuple[str, str]:
    """Returns (text, model). Raises on any API problem or refusal."""
    import anthropic

    model = settings.llm_model or DEFAULT_MODEL
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    extra: dict[str, Any] = {}
    if model in FALLBACK_MODELS:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    resp = client.with_options(timeout=timeout_s, max_retries=0).beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "Fact sheet (JSON):\n" + json.dumps(sheet, ensure_ascii=False, indent=1)
                   + "\n\nWrite the explanation now."}],
        **extra,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused")
    text = " ".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty reply (stop_reason={resp.stop_reason})")
    return text, resp.model


def _call_sap_genai_hub(sheet: dict[str, Any], settings: Settings, timeout_s: float) -> tuple[str, str]:
    # STUB: SAP Generative AI Hub is not available to the team (plan §7). Designed to plug in here:
    # same fact sheet + SYSTEM_PROMPT, same number check afterwards.
    raise NotImplementedError("sap_genai_hub provider is a stub (not implemented)")


PROVIDERS: dict[str, Callable[[dict[str, Any], Settings, float], tuple[str, str]]] = {
    "anthropic": _call_anthropic,
    "sap_genai_hub": _call_sap_genai_hub,
}
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="explain")


# ---------------------------------------------------------------- entry point


def explain(forecast: Forecast, dependency: DependencyProfile | None = None,
            recommendation: Recommendation | None = None, *, name: str | None = None,
            settings: Settings | None = None, timeout_s: float = TIMEOUT_S) -> Explanation:
    """LLM narration of the fact sheet if it passes the number check, else the deterministic template."""
    s = settings or load_settings()
    sheet = fact_sheet(forecast, dependency, recommendation, name)
    fallback = template(sheet)
    provider = (s.llm_provider or "none").lower()
    if provider == "none":
        return Explanation(text=fallback, used_llm=False, fallback_reason="LLM_PROVIDER=none", provider=provider)
    call = PROVIDERS.get(provider)
    if call is None:
        return Explanation(text=fallback, used_llm=False, fallback_reason=f"unknown LLM_PROVIDER {provider!r}",
                           provider=provider)

    fut = _POOL.submit(call, sheet, s, timeout_s)
    try:
        text, model = fut.result(timeout=timeout_s)  # hard wall clock, whatever the SDK does
    except concurrent.futures.TimeoutError:
        return Explanation(text=fallback, used_llm=False, fallback_reason=f"timeout after {timeout_s:g}s",
                           provider=provider)
    except Exception as exc:  # any provider failure -> template; never leak keys or payloads
        log.warning("[explain] %s failed: %s", provider, type(exc).__name__)
        return Explanation(text=fallback, used_llm=False, fallback_reason=f"{provider} error: {type(exc).__name__}",
                           provider=provider)

    claims = intent_claims(text)
    if claims:
        log.warning("[explain] discarded LLM text: claims manufacturer intent: %s", claims[:3])
        return Explanation(text=fallback, used_llm=False, provider=provider, model=model,
                           fallback_reason="intent claim: " + ", ".join(claims[:3]))
    bad = unsupported_numbers(text, sheet)
    if bad:
        log.warning("[explain] discarded LLM text: numbers not in the fact sheet: %s", bad[:5])
        return Explanation(text=fallback, used_llm=False, provider=provider, model=model,
                           fallback_reason="number check failed: " + ", ".join(bad[:5]))
    return Explanation(text=text, used_llm=True, provider=provider, model=model)
