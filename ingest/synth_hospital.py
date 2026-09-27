"""Synthetic, S/4-shaped hospital pharmacy data (plan §5, §6). Every row is IS_PROXY='SYNTH'.

    python -m ingest.synth_hospital      # write data/seed/FF_MM_*.csv and FF_CFG_PARAM.csv

One plant (H001) with five storage locations, shaped on the pharmacist's SAP SOP:
CENT central pharmacy (receives all goods, OPD), ABLK / BBLK / CBLK ward blocks, ONCO oncology.
A seeded weekly simulation over 18 months ending at AS_OF produces one consistent history:
  101 GR at CENT against POs (EKKO/EKPO; one vendor's on-time delivery drops in the last quarter),
  122 part-returns to vendor, 311 stock transfers CENT -> blocks (two lines: 1 = issuing, 2 = receiving),
  261 FEFO issues to ward cost centres, 551 scrap of expired stock.
FF_MM_MCHB is the batch stock left at AS_OF (some near expiry, a few batches in quality inspection).
ABC / FSN classes are computed from the simulated consumption; VED from the target category.
Exactly one batch carries the batch number and manufacturer of a REAL CDSCO NSQ alert (FF_REF_NSQ_ALERT)
so the recall-trace demo has a genuine hit; no other batch number can collide with an NSQ batch.
Vendors are fictional distributors, named "(SYNTH)"; nothing here describes a real hospital or firm.
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from agents.ctx import REPO_ROOT

SEED_DIR = REPO_ROOT / "data" / "seed"
SEED = 20260927
AS_OF = date(2026, 9, 25)
MONTHS = 18
PLANT = "H001"
LOCATIONS = {  # LGORT -> (name, share of hospital demand, ward cost centre)
    "CENT": ("Central Pharmacy", 0.40, "OPD-PHARM"),
    "ABLK": ("A Block", 0.20, "WARD-A"),
    "BBLK": ("B Block", 0.20, "WARD-B"),
    "CBLK": ("C Block", 0.15, "WARD-C"),
    "ONCO": ("Oncology", 0.05, "WARD-ONC"),
}
VENDORS = (  # LIFNR, name, lead days, (mean delay, sd), notes
    ("V100001", "Kaveri Pharma Distributors (SYNTH)", 7, (-1.0, 1.5)),
    ("V100002", "Noyyal Medical Agencies (SYNTH)", 10, (-1.5, 2.0)),
    ("V100003", "Bhavani Healthcare Supplies (SYNTH)", 12, (-1.0, 1.5)),  # OTD drops in the last quarter
    ("V100004", "Siruvani Drug House (SYNTH)", 14, (-0.5, 2.5)),
    ("V100005", "Amaravathi Surgicals & Pharma (SYNTH)", 9, (-1.5, 1.5)),
    ("V100006", "Palar Pharma Traders (SYNTH)", 10, (3.0, 3.0)),          # blacklisted after month 6
)
DETERIORATING_VENDOR, BLACKLISTED_VENDOR = "V100003", "V100006"
COLD_KEYWORDS = ("vaccine", "immunoglobulin", "oxytocin")
ONCOLOGY_KEYWORDS = ("carboplatin", "cisplatin")
PROV = {"SOURCE": f"synth_hospital seed {SEED}", "SOURCE_URL": "",
        "FETCHED_AT": f"{AS_OF.isoformat()}T00:00:00Z", "IS_PROXY": "SYNTH"}


def _rng(*parts: object) -> np.random.Generator:
    digest = hashlib.sha256("|".join(map(str, (SEED,) + parts)).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def norm_batch(b: str) -> str:
    return re.sub(r"\s+", "", str(b or "")).upper()


@dataclass
class Batch:
    charg: str
    vfdat: date
    qty: float
    mfr: str | None


@dataclass
class Material:
    matnr: str
    target: dict[str, str]
    meins: str
    daily: float                 # hospital-wide mean daily demand
    locations: list[str]
    shelf_months: int
    pack: int
    cold: bool
    price: float                 # net price paid per unit (<= ceiling)
    vendors: tuple[str, str]     # primary, secondary
    makers: list[str]
    stock: dict[str, list[Batch]] = field(default_factory=dict)


class Sim:
    """Collects rows; keeps document numbers unique and deterministic."""

    def __init__(self) -> None:
        self.mseg: list[dict[str, Any]] = []
        self.ekko: list[dict[str, Any]] = []
        self.ekpo: list[dict[str, Any]] = []
        self.doc_no = 4900000000
        self.po_no = 4500000000
        self.batch_no = 0

    def doc(self) -> str:
        self.doc_no += 1
        return str(self.doc_no)

    def new_charg(self, when: date) -> str:
        self.batch_no += 1
        return f"SY{when:%y%m}{self.batch_no:05d}"  # 'SY' prefix: never a real batch number

    def move(self, doc: str, line: int, bwart: str, m: Material, lgort: str, charg: str | None, qty: float,
             when: date, **extra: Any) -> None:
        self.mseg.append({"MBLNR": doc, "MJAHR": when.year, "ZEILE": line, "BWART": bwart, "MATNR": m.matnr,
                          "WERKS": PLANT, "LGORT": lgort, "CHARG": charg or "", "MENGE": _q(qty),
                          "MEINS": m.meins, "BUDAT": when.isoformat(), "UMWRK": extra.get("umwrk", ""),
                          "KOSTL": extra.get("kostl", ""), "LIFNR": extra.get("lifnr", ""),
                          "EBELN": extra.get("ebeln", ""), "EBELP": extra.get("ebelp", ""), **PROV})


def _q(x: float) -> float:
    return round(float(x), 3)


def _classify_unit(unit: str, form: str) -> tuple[str, int, int]:
    """(MEINS, shelf-life months, order pack size) from the NPPA unit / dosage form."""
    u, f = unit.lower(), form.lower()
    if "ml" in u and "vial" not in u and "dose" not in u:
        return "ML", 24, 100
    if "tablet" in u or "capsule" in u:
        return "EA", 36, 100
    return "EA", 24, 10


def _daily_demand(rng: np.random.Generator, meins: str, target: dict[str, str]) -> float:
    cat, unit = target["CATEGORY"].lower(), target["UNIT"].lower()
    if "vaccine" in cat:
        return float(rng.uniform(2, 10))
    if "oncology" in cat:
        return float(rng.uniform(4, 20))
    if meins == "ML":
        return float(rng.uniform(8, 60))
    if "tablet" in unit or "capsule" in unit:
        return float(rng.uniform(40, 400))
    return float(rng.uniform(3, 25))


def _ved(target: dict[str, str]) -> str:
    cat, gen = target["CATEGORY"].lower(), target["GENERIC"].lower()
    if any(k in cat for k in ("vaccine", "injectable")) or "immunoglobulin" in gen:
        return "V"
    if any(k in cat for k in ("tb", "cardiac", "anti-malarial", "old antibiotic", "respiratory")):
        return "E"
    return "D"


def build_materials(targets: list[dict[str, str]], producers: dict[str, list[str]],
                    ceiling: dict[str, float]) -> list[Material]:
    mats = []
    for i, t in enumerate(sorted(targets, key=lambda r: r["FORM_ID"])):
        rng = _rng("material", t["FORM_ID"])
        meins, shelf, pack = _classify_unit(t["UNIT"], t["DOSAGE_FORM"])
        gen = t["GENERIC"].lower()
        onco = any(k in gen for k in ONCOLOGY_KEYWORDS)
        if onco:
            locs = ["CENT", "ONCO"]
        else:
            locs = ["CENT"] + [l for l in ("ABLK", "BBLK", "CBLK", "ONCO") if rng.random() < (0.35 if l == "ONCO" else 0.75)]
        primary = VENDORS[int(rng.integers(0, len(VENDORS)))][0]
        secondary = VENDORS[int(rng.integers(0, len(VENDORS) - 1))][0]
        if secondary == primary:
            secondary = VENDORS[-2][0] if primary != VENDORS[-2][0] else VENDORS[0][0]
        mats.append(Material(
            matnr=f"M{100001 + i}", target=t, meins=meins, daily=_daily_demand(rng, meins, t), locations=locs,
            shelf_months=shelf, pack=pack, cold=any(k in gen for k in COLD_KEYWORDS),
            price=round(ceiling[t["FORM_ID"]] * float(rng.uniform(0.55, 0.85)), 4),
            vendors=(primary, secondary), makers=sorted(producers.get(t["FORM_ID"], []))))
    return mats


def _loc_share(m: Material) -> dict[str, float]:
    raw = {l: LOCATIONS[l][1] for l in m.locations}
    total = sum(raw.values())
    return {l: s / total for l, s in raw.items()}


def _consume(batches: list[Batch], qty: float, today: date) -> list[tuple[Batch, float]]:
    """FEFO: take from earliest-expiry unexpired batches. Returns (batch, qty taken) pairs."""
    taken = []
    for b in sorted(batches, key=lambda b: (b.vfdat, b.charg)):
        if qty <= 1e-9:
            break
        if b.vfdat <= today or b.qty <= 1e-9:
            continue
        t = min(b.qty, qty)
        b.qty -= t
        qty -= t
        taken.append((b, t))
    batches[:] = [b for b in batches if b.qty > 1e-9]
    return taken


def simulate(m: Material, sim: Sim, start: date, planted: dict[str, Any] | None) -> None:
    rng = _rng("sim", m.matnr)
    share = _loc_share(m)
    m.stock = {l: [] for l in m.locations}
    # opening stock (received before the window; no 101 in MSEG): ~45 days at CENT, ~14 days per block
    for l in m.locations:
        days = 45 if l == "CENT" else 14
        mfd = start - timedelta(days=int(rng.integers(60, 240)))
        m.stock[l].append(Batch(sim.new_charg(mfd), mfd + timedelta(days=30 * m.shelf_months),
                                math.ceil(m.daily * share[l] * days), _maker(m, rng)))
    # the NSQ batch arrives with the first GR in the last 60 days; made before the alert, so it has the
    # earliest expiry and FEFO transfers spread it to the blocks before AS_OF
    plant_from = AS_OF - timedelta(days=60)

    open_pos: list[dict[str, Any]] = []
    week = start - timedelta(days=start.weekday())
    blacklist_from = start + timedelta(days=183)
    while week <= AS_OF:
        # goods receipts due this week (and part-returns)
        for po in sorted((p for p in open_pos if p["gr"] < week + timedelta(days=7) and p["gr"] <= AS_OF),
                        key=lambda p: p["gr"]):
            open_pos.remove(po)
            short = rng.random() < 0.08
            # 8% arrive short-dated (2-7 months of life left): near-expiry stock and some 551 scrap
            age = 30 * m.shelf_months - int(rng.integers(60, 210)) if short else int(rng.integers(20, 120))
            mfd = po["gr"] - timedelta(days=age)
            b = Batch(sim.new_charg(po["gr"]), mfd + timedelta(days=30 * m.shelf_months), po["qty"], _maker(m, rng))
            if planted and po["gr"] >= plant_from:
                b = Batch(planted["BATCH"], planted["MFD"] + timedelta(days=30 * m.shelf_months), po["qty"],
                          planted["MANUFACTURER_ID"])
                planted = None
            m.stock["CENT"].append(b)
            sim.move(sim.doc(), 1, "101", m, "CENT", b.charg, po["qty"], po["gr"], lifnr=po["lifnr"],
                     ebeln=po["ebeln"], ebelp=10)
            if rng.random() < 0.03:
                back = math.ceil(po["qty"] * float(rng.uniform(0.05, 0.2)))
                b.qty -= back
                sim.move(sim.doc(), 1, "122", m, "CENT", b.charg, back, po["gr"] + timedelta(days=2),
                         lifnr=po["lifnr"], ebeln=po["ebeln"], ebelp=10)
        # scrap anything expired
        for l in m.locations:
            for b in [b for b in m.stock[l] if b.vfdat <= week]:
                sim.move(sim.doc(), 1, "551", m, l, b.charg, b.qty, week)
                m.stock[l].remove(b)
        # issues to wards (FEFO), mid-week
        season = 1.0 + 0.15 * math.sin(2 * math.pi * (week.timetuple().tm_yday / 365.0))
        for l in m.locations:
            day = week + timedelta(days=3)
            if day > AS_OF:
                continue
            want = float(rng.poisson(max(0.1, 7 * m.daily * share[l] * season)))
            doc, line = sim.doc(), 0
            for b, q in _consume(m.stock[l], want, day):
                line += 1
                sim.move(doc, line, "261", m, l, b.charg, q, day, kostl=LOCATIONS[l][2])
        # four-weekly transfers CENT -> blocks, topping up to 35 days
        if (week - start).days // 7 % 4 == 0 and week + timedelta(days=1) <= AS_OF:
            for l in [l for l in m.locations if l != "CENT"]:
                have = sum(b.qty for b in m.stock[l])
                need = math.ceil(max(0.0, 35 * m.daily * share[l] - have))
                for b, q in _consume(m.stock["CENT"], need, week + timedelta(days=1)):
                    doc = sim.doc()
                    sim.move(doc, 1, "311", m, "CENT", b.charg, q, week + timedelta(days=1), umwrk=PLANT)
                    sim.move(doc, 2, "311", m, l, b.charg, q, week + timedelta(days=1))
                    m.stock[l].append(Batch(b.charg, b.vfdat, q, b.mfr))
        # reorder at CENT: position < 30 days of hospital demand -> order 60 days
        position = sum(b.qty for l in m.locations for b in m.stock[l]) + sum(p["qty"] for p in open_pos)
        if position < 30 * m.daily:
            lifnr = m.vendors[0] if rng.random() < 0.75 else m.vendors[1]
            if lifnr == BLACKLISTED_VENDOR and week >= blacklist_from:
                lifnr = VENDORS[0][0]
            _, _, lead, (mu, sd) = next(v for v in VENDORS if v[0] == lifnr)
            if lifnr == DETERIORATING_VENDOR and week >= AS_OF - timedelta(days=100):
                mu, sd = 9.0, 3.0
            eindt = week + timedelta(days=lead)
            gr = max(week + timedelta(days=2), eindt + timedelta(days=int(round(rng.normal(mu, sd)))))
            qty = m.pack * math.ceil(60 * m.daily / m.pack)
            sim.po_no += 1
            po = {"ebeln": str(sim.po_no), "lifnr": lifnr, "qty": qty, "gr": gr}
            open_pos.append(po)
            sim.ekko.append({"EBELN": po["ebeln"], "BSART": "NB", "LIFNR": lifnr, "BEDAT": week.isoformat(),
                             "WAERS": "INR", **PROV})
            sim.ekpo.append({"EBELN": po["ebeln"], "EBELP": 10, "MATNR": m.matnr, "WERKS": PLANT, "MENGE": qty,
                             "MEINS": m.meins, "NETPR": m.price, "PEINH": 1, "EINDT": eindt.isoformat(), **PROV})
        week += timedelta(days=7)


def _maker(m: Material, rng: np.random.Generator) -> str | None:
    return m.makers[int(rng.integers(0, len(m.makers)))] if m.makers else None


def choose_nsq_alert(nsq: list[dict[str, str]], target_ids: set[str]) -> dict[str, str]:
    """The NSQ alert to plant: earliest alert for a target formulation that has a batch and a manufacturer."""
    hits = [a for a in nsq if a.get("FORM_ID") in target_ids and a.get("BATCH") and a.get("MANUFACTURER_ID")]
    if not hits:
        raise ValueError("no CDSCO NSQ alert matches a target formulation; cannot plant the recall batch")
    return sorted(hits, key=lambda a: (a["MONTH"], a["ALERT_ID"]))[0]


def generate(targets: list[dict[str, str]], producers: dict[str, list[str]], ceiling: dict[str, float],
             nsq: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """All FF_MM_* rows plus FF_CFG_PARAM. Deterministic for the same inputs."""
    months_back = AS_OF.year * 12 + AS_OF.month - 1 - MONTHS
    start = date(months_back // 12, months_back % 12 + 1, min(AS_OF.day, 28))
    mats = build_materials(targets, producers, ceiling)
    alert = choose_nsq_alert(nsq, {t["FORM_ID"] for t in targets})
    alert_month = date.fromisoformat(alert["MONTH"][:10])
    planted = {"FORM_ID": alert["FORM_ID"], "BATCH": alert["BATCH"].strip(), "MANUFACTURER_ID": alert["MANUFACTURER_ID"],
               "MFD": alert_month - timedelta(days=60)}

    sim = Sim()
    for m in mats:
        simulate(m, sim, start, planted if m.target["FORM_ID"] == planted["FORM_ID"] else None)

    nsq_batches = {norm_batch(a["BATCH"]) for a in nsq if a.get("BATCH")}
    stock: dict[tuple[str, str, str], dict[str, Any]] = {}   # one MCHB row per (material, location, batch)
    mara: list[dict[str, Any]] = []
    for m in mats:
        for l in m.locations:
            for b in m.stock[l]:
                if norm_batch(b.charg) in nsq_batches and b.charg != planted["BATCH"]:
                    raise AssertionError(f"generated batch {b.charg} collides with an NSQ batch")
                row = stock.setdefault((m.matnr, l, b.charg), {
                    "MATNR": m.matnr, "WERKS": PLANT, "LGORT": l, "CHARG": b.charg, "CLABS": 0.0, "CINSM": 0,
                    "CSPEM": 0, "VFDAT": b.vfdat.isoformat(), "HSDAT": "", "MANUFACTURER_ID": b.mfr or "", **PROV})
                row["CLABS"] = _q(row["CLABS"] + b.qty)
    mchb = list(stock.values())
    planted_rows = [r for r in mchb if r["CHARG"] == planted["BATCH"]]
    if len(planted_rows) < 2:
        raise AssertionError("the planted NSQ batch must remain at two or more locations at AS_OF")
    # a few batches under quality inspection (quarantine) at AS_OF, never the planted one
    qrng = _rng("quarantine")
    others = [r for r in mchb if r["CHARG"] != planted["BATCH"] and r["CLABS"] > 0]
    for i in sorted(qrng.choice(len(others), size=min(3, len(others)), replace=False)):
        others[i]["CINSM"], others[i]["CLABS"] = others[i]["CLABS"], 0

    abc, fsn = _abc_fsn(mats, sim.mseg)
    for m in mats:
        t = m.target
        mara.append({"MATNR": m.matnr, "MAKTX": f"{t['GENERIC']} {t['STRENGTH']} {t['DOSAGE_FORM']}".upper()[:200],
                     "GENERIC": t["GENERIC"], "STRENGTH": t["STRENGTH"], "MEINS": m.meins, "VED": _ved(t),
                     "ABC": abc[m.matnr], "FSN": fsn[m.matnr], "RAUBE": "COLD 2-8C" if m.cold else "AMBIENT",
                     "XCHPF": "X", "FORM_ID": t["FORM_ID"], **PROV})

    lfa1 = []
    for i, (lifnr, name, _, _) in enumerate(VENDORS, 1):
        lfa1.append({"LIFNR": lifnr, "NAME1": name, "GSTIN": f"33ZZSYN{i:04d}Z1Z{i}",
                     "DRUG_LICENCE_NO": f"TN-CBE-SYN-20B-{i:04d}", "SPERR": "X" if lifnr == BLACKLISTED_VENDOR else "",
                     **PROV})
    t001w = [{"WERKS": PLANT, "NAME1": "Demo Hospital Pharmacy (SYNTH)", "ORT01": "Coimbatore", "REGIO": "TN",
              "LOC_TYPE": "Hospital (5 SLocs)",  # CENT, ABLK, BBLK, CBLK, ONCO: see LOCATIONS
              **PROV}]
    cold = _coldchain()
    cfg = [{"NAME": "AS_OF_DATE", "DATE_VALUE": AS_OF.isoformat(), "TEXT_VALUE": "synthetic hospital data end date"}]
    return {"FF_MM_T001W": t001w, "FF_MM_LFA1": lfa1, "FF_MM_MARA": mara, "FF_MM_MCHB": mchb, "FF_MM_MSEG": sim.mseg,
            "FF_MM_EKKO": sim.ekko, "FF_MM_EKPO": sim.ekpo, "FF_MM_COLDCHAIN": cold, "FF_CFG_PARAM": cfg}


def _abc_fsn(mats: list[Material], mseg: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """ABC by 12-month issue value (A = top 70% cumulative, B = next 20%); FSN by issue-quantity tercile."""
    since = (AS_OF - timedelta(days=365)).isoformat()
    qty = {m.matnr: 0.0 for m in mats}
    for r in mseg:
        if r["BWART"] == "261" and r["BUDAT"] > since:
            qty[r["MATNR"]] += r["MENGE"]
    value = {m.matnr: qty[m.matnr] * m.price for m in mats}
    total = sum(value.values()) or 1.0
    abc, cum = {}, 0.0
    for matnr, v in sorted(value.items(), key=lambda kv: -kv[1]):
        cum += v / total
        abc[matnr] = "A" if cum <= 0.70 else "B" if cum <= 0.90 else "C"
    ranked = sorted(qty, key=lambda k: -qty[k])
    n = len(ranked)
    fsn = {k: "F" if i < n / 3 else "S" if i < 2 * n / 3 else "N" for i, k in enumerate(ranked)}
    return abc, fsn


def _coldchain() -> list[dict[str, Any]]:
    """Fridge loggers per storage location every 6 h for 180 days; exactly two excursions (ABLK, ONCO)."""
    rng = _rng("coldchain")
    excursions = {("ABLK", AS_OF - timedelta(days=40)): 11.4, ("ONCO", AS_OF - timedelta(days=9)): 9.3}
    rows = []
    for l in LOCATIONS:
        for d in range(180, 0, -1):
            day = AS_OF - timedelta(days=d)
            for h in (0, 6, 12, 18):
                temp = float(np.clip(rng.normal(5.0, 0.7), 2.4, 7.6))
                if h == 12 and (l, day) in excursions:
                    temp = excursions[(l, day)]
                rows.append({"LOCATION": l, "TS": f"{day.isoformat()} {h:02d}:00:00", "TEMP_C": round(temp, 2),
                             "EXCURSION_FLAG": int(temp < 2 or temp > 8), **PROV})
    return rows


# ---------------------------------------------------------------- CLI


def _read(name: str) -> list[dict[str, str]]:
    with (SEED_DIR / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_inputs() -> tuple[list[dict[str, str]], dict[str, list[str]], dict[str, float], list[dict[str, str]]]:
    targets = _read("targets.csv")
    producers: dict[str, list[str]] = {}
    if (SEED_DIR / "FF_REF_PRODUCER.csv").exists():
        for r in _read("FF_REF_PRODUCER.csv"):
            producers.setdefault(r["FORM_ID"], []).append(r["MANUFACTURER_ID"])
    ceiling = {r["FORM_ID"]: float(r["CEILING_PRICE"])
               for r in sorted(_read("FF_REF_CEILING_PRICE.csv"), key=lambda r: r["EFFECTIVE_FROM"])}
    nsq = _read("FF_REF_NSQ_ALERT.csv")
    return targets, producers, ceiling, nsq


def write_seeds(tables: dict[str, list[dict[str, Any]]], seed_dir: Path = SEED_DIR) -> dict[str, int]:
    counts = {}
    for name, rows in tables.items():
        with (seed_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        counts[name] = len(rows)
    return counts


def main() -> int:
    counts = write_seeds(generate(*load_inputs()))
    print(" | ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
