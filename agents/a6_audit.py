"""A6 Compliance, Audit & Action (plan §3 A6, §7 API Hub row).

Observer: `audit()` appends one hash-chained row per agent hop to FF_AG_AUDIT_LOG
(input/output hashes, rule-file versions = YAML version + sha256 of the file, data sources +
fetched_at, PREV_HASH, ROW_HASH). The chain is per run: seq 0, 1, 2 … and PREV_HASH = the
previous ROW_HASH. This module never UPDATEs or DELETEs the log.

Action: `act(rec_id)` posts an S/4-shaped purchase requisition (API_PURCHASEREQ_PROCESS_SRV,
A_PurchaseRequisitionHeader + to_PurchaseReqnItem) to the FF_MM_EBAN mock, and only when
FF_AG_APPROVAL holds an approving row with every checklist item ticked (plus a distinct
level-2 approver when one is needed) and the run's audit chain verifies.

`rec_id` is the A4/A5 scenario id (`<run_id>:<form_id>:<nn>`); it already names the run.
Master data is optionally checked read-only against the SAP API Business Hub sandbox; any
problem there is a WARN, never a block.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
from pydantic import BaseModel

from agents.common import placeholders, to_tag, try_query
from agents.contracts import (
    ApprovalAction, ApprovalChecklist, AuditEvent, CheckStatus, DataSourceRef, DraftPR, Overall,
)
from agents.ctx import Db
from agents.rules import RULES_DIR, Rules

log = logging.getLogger(__name__)

AGENT = "A6"
AUDIT_COLS = ("RUN_ID", "SEQ", "AGENT", "TS", "INPUT_HASH", "OUTPUT_HASH", "RULE_VERSIONS_JSON",
              "DATA_SOURCES_JSON", "PREV_HASH", "ROW_HASH")
SOURCES_JSON_MAX = 4000  # FF_AG_AUDIT_LOG.DATA_SOURCES_JSON NVARCHAR(4000)

# Input tables per hop, used to record data sources + fetched_at (plan §3 A1–A5 inputs).
AGENT_SOURCES: dict[str, tuple[str, ...]] = {
    "A1": ("FF_REF_CEILING_PRICE", "FF_REF_API_COST_MONTHLY", "FF_REF_WPI", "FF_REF_BOM_ASSUMPTION",
           "FF_REF_NSQ_ALERT"),
    "A2": ("FF_REF_PRODUCER", "FF_REF_API_ORIGIN", "FF_MM_MARA"),
    "A3": ("FF_MM_MCHB", "FF_MM_MSEG", "FF_REF_FORMULATION"),
    "A4": ("FF_MM_EKPO", "FF_MM_LFA1", "FF_MM_MCHB", "FF_REF_PRODUCER"),
    "A5": ("FF_REF_CEILING_PRICE", "FF_REF_FORMULATION", "FF_MM_LFA1", "FF_REF_NSQ_ALERT"),
}

# S/4HANA API_PURCHASEREQ_PROCESS_SRV (OData V2) constants for the mock PR.
S4_SERVICE = "API_PURCHASEREQ_PROCESS_SRV"
S4_ENTITY = "A_PurchaseRequisitionHeader"
PR_DOC_TYPE = "NB"          # standard purchase requisition
PR_ITEM_NO = 10
BANFN_PREFIX = "FF"
EBAN_STATUS = "POSTED_MOCK"

API_HUB_BASE = "https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap"
API_HUB_TIMEOUT_S = 6

ACTION_TABLE = "FF_AG_ACTION"  # db/schema.sql (S01 is the only DDL)
ACTION_COLS = ("RUN_ID", "SCENARIO_ID", "BANFN", "POSTED_AT", "POSTED_BY", "S4_SERVICE", "PAYLOAD_JSON",
               "PAYLOAD_HASH", "MASTERDATA_JSON")
EBAN_COLS = ("BANFN", "BNFPO", "MATNR", "WERKS", "MENGE", "MEINS", "PREIS", "PEINH", "BADAT", "LFDAT", "FLIEF",
             "AFNAM", "STATUS", "RUN_ID", "SCENARIO_ID", "SOURCE", "SOURCE_URL", "FETCHED_AT", "IS_PROXY")


class ActRefused(PermissionError):
    """A6 refused to act (no/insufficient approval, blocked recommendation, broken chain, already posted)."""


# ---------------------------------------------------------------- hashing


def _jsonable(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json", by_alias=False)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, pd.DataFrame):
        return o.to_dict(orient="records")
    if isinstance(o, Path):
        return o.as_posix()
    raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


def canonical(obj: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace) used for every hash."""
    return json.dumps(obj, default=_jsonable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(obj: Any) -> str:
    data = obj if isinstance(obj, bytes) else canonical(obj).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def rule_file_versions(rules: Rules | None = None, rules_dir: str | Path = RULES_DIR) -> dict[str, str]:
    """{file: "<yaml version>+sha256:<hash of the file bytes>"} for every rules/*.yaml."""
    out = {}
    for path in sorted(Path(rules_dir).glob("*.yaml")):
        ver = rules.docs.get(path.stem, {}).get("version", "?") if rules else "?"
        out[path.stem] = f"{ver}+sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return out


def _ts(v: Any) -> str:
    """Normalise a timestamp to naive-UTC ISO with microseconds (same string on SQLite and HANA)."""
    if v is None:
        return ""
    if not isinstance(v, datetime):
        v = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v.isoformat(timespec="microseconds")


def row_hash(run_id: str, seq: int, agent: str, ts: Any, input_hash: str, output_hash: str,
             rule_versions_json: str, data_sources_json: str, prev_hash: str | None) -> str:
    """sha256 over the stored column values, so the chain can be re-verified from the table alone."""
    parts = (run_id, str(int(seq)), agent, _ts(ts), input_hash, output_hash, rule_versions_json or "",
             data_sources_json or "", prev_hash or "")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- data sources


def _nz(v: Any) -> Any:
    """None for NULL / NaN / empty (pandas turns NULL into NaN in mixed columns)."""
    return None if v is None or v == "" or (isinstance(v, float) and pd.isna(v)) else v


def _as_dt(v: Any) -> datetime | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    try:
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def data_sources(db: Db, tables: Iterable[str]) -> list[DataSourceRef]:
    """Distinct (SOURCE, SOURCE_URL, IS_PROXY) with the latest FETCHED_AT across the given tables."""
    refs: list[DataSourceRef] = []
    for t in tables:
        try:
            df = try_query(db, f"SELECT SOURCE, SOURCE_URL, IS_PROXY, MAX(FETCHED_AT) AS FETCHED_AT FROM {t} "
                               "GROUP BY SOURCE, SOURCE_URL, IS_PROXY")
        except Exception as exc:  # table without provenance columns: record nothing rather than fail the run
            log.warning("[a6] no provenance for %s: %s", t, exc)
            continue
        for r in (df.to_dict(orient="records") if df is not None else []):
            refs.append(DataSourceRef(source=f"{t}:{r['SOURCE']}", source_url=r["SOURCE_URL"] or None,
                                      fetched_at=_as_dt(r["FETCHED_AT"]), is_proxy=to_tag(r["IS_PROXY"])))
    return refs


def _sources_json(refs: Sequence[DataSourceRef]) -> str:
    items = sorted({canonical(r): r for r in refs}.items())
    kept: list[Any] = []
    for _, r in items:
        trial = canonical(kept + [r])
        if len(trial) > SOURCES_JSON_MAX - 64:
            log.warning("[a6] data sources truncated to %d of %d to fit the column", len(kept), len(items))
            kept.append({"truncated": len(items) - len(kept)})
            break
        kept.append(r)
    return canonical(kept)


# ---------------------------------------------------------------- audit log


class MemoryAuditLog:
    """In-memory sink with the same chaining (dry runs write nothing to the DB)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def last(self, run_id: str) -> tuple[int, str | None]:
        mine = [r for r in self.rows if r["RUN_ID"] == run_id]
        return (int(mine[-1]["SEQ"]) + 1, mine[-1]["ROW_HASH"]) if mine else (0, None)

    def insert(self, row: dict[str, Any]) -> None:
        self.rows.append(row)

    def load(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows if r["RUN_ID"] == run_id]


class DbAuditLog:
    """FF_AG_AUDIT_LOG sink. INSERT and SELECT only."""

    def __init__(self, db: Db) -> None:
        self.db = db

    def last(self, run_id: str) -> tuple[int, str | None]:
        df = self.db.query("SELECT SEQ, ROW_HASH FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? AND SEQ = "
                           "(SELECT MAX(SEQ) FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ?)", (run_id, run_id))
        return (int(df.iloc[0]["SEQ"]) + 1, str(df.iloc[0]["ROW_HASH"])) if len(df) else (0, None)

    def insert(self, row: dict[str, Any]) -> None:
        if self.db.backend == "sqlite":
            row = {**row, "TS": _ts(row["TS"])}
        self.db.execute(f"INSERT INTO FF_AG_AUDIT_LOG ({', '.join(AUDIT_COLS)}) VALUES ({placeholders(AUDIT_COLS)})",
                        [row[c] for c in AUDIT_COLS])

    def load(self, run_id: str) -> list[dict[str, Any]]:
        df = self.db.query(f"SELECT {', '.join(AUDIT_COLS)} FROM FF_AG_AUDIT_LOG WHERE RUN_ID = ? ORDER BY SEQ",
                           (run_id,))
        return df.to_dict(orient="records")


def _sink(target: Db | MemoryAuditLog | DbAuditLog) -> MemoryAuditLog | DbAuditLog:
    return DbAuditLog(target) if isinstance(target, Db) else target


def audit(event: AuditEvent, sink: Db | MemoryAuditLog | DbAuditLog) -> AuditEvent:
    """Append one event. SEQ, PREV_HASH and ROW_HASH are assigned here from the run's last row."""
    s = _sink(sink)
    seq, prev = s.last(event.run_id)
    ts = event.ts.astimezone(timezone.utc).replace(tzinfo=None) if event.ts.tzinfo else event.ts
    rv_json = canonical(event.rule_versions)
    src_json = _sources_json(event.data_sources)
    h = row_hash(event.run_id, seq, event.agent, ts, event.input_hash, event.output_hash, rv_json, src_json, prev)
    s.insert({"RUN_ID": event.run_id, "SEQ": seq, "AGENT": event.agent, "TS": ts, "INPUT_HASH": event.input_hash, "OUTPUT_HASH": event.output_hash,
              "RULE_VERSIONS_JSON": rv_json, "DATA_SOURCES_JSON": src_json, "PREV_HASH": prev, "ROW_HASH": h})
    return event.model_copy(update={"seq": seq, "prev_hash": prev, "event_hash": h, "ts": ts.replace(tzinfo=timezone.utc)})


def hop(sink: Db | MemoryAuditLog | DbAuditLog, run_id: str, agent: str, inp: Any, out: Any,
        sources: Sequence[DataSourceRef] = (), rule_versions: dict[str, str] | None = None) -> AuditEvent:
    """Hash an agent's input and output and append the audit event."""
    ev = AuditEvent(run_id=run_id, seq=0, agent=agent, input_hash=sha256_hex(inp), output_hash=sha256_hex(out),
                    rule_versions=rule_versions or {}, data_sources=list(sources))
    return audit(ev, sink)


@dataclass
class ChainCheck:
    ok: bool
    n_rows: int
    broken_at: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        return f"chain ok ({self.n_rows} rows)" if self.ok else f"chain BROKEN at seq {self.broken_at}: {self.reason}"


def verify_rows(rows: Sequence[dict[str, Any]]) -> ChainCheck:
    """Recompute every ROW_HASH and PREV_HASH link; report the first bad SEQ."""
    prev: str | None = None
    for i, r in enumerate(rows):
        seq = int(r["SEQ"])
        if seq != i:
            return ChainCheck(False, len(rows), i, f"expected seq {i}, found {seq} (row missing or inserted)")
        if _nz(r["PREV_HASH"]) != prev:
            return ChainCheck(False, len(rows), seq, "PREV_HASH does not match the previous ROW_HASH")
        h = row_hash(r["RUN_ID"], seq, r["AGENT"], r["TS"], r["INPUT_HASH"], r["OUTPUT_HASH"],
                     _nz(r["RULE_VERSIONS_JSON"]), _nz(r["DATA_SOURCES_JSON"]), _nz(r["PREV_HASH"]))
        if h != r["ROW_HASH"]:
            return ChainCheck(False, len(rows), seq, "ROW_HASH does not match the row contents (tampered)")
        prev = r["ROW_HASH"]
    return ChainCheck(True, len(rows))


def verify_chain(run_id: str, sink: Db | MemoryAuditLog | DbAuditLog) -> ChainCheck:
    """ok, or the first SEQ where the run's chain breaks. An empty chain is not ok."""
    rows = _sink(sink).load(run_id)
    if not rows:
        return ChainCheck(False, 0, 0, "no audit rows for this run")
    return verify_rows(rows)


# ---------------------------------------------------------------- recommendation + approvals


def split_rec_id(rec_id: str) -> tuple[str, str]:
    """rec_id is the scenario id `<run_id>:<form_id>:<nn>`; returns (run_id, scenario_id)."""
    parts = rec_id.split(":")
    if len(parts) < 3:
        raise ValueError(f"not a recommendation id (expected <run_id>:<form_id>:<nn>): {rec_id!r}")
    return ":".join(parts[:-2]), rec_id


def load_recommendation(db: Db, rec_id: str) -> dict[str, Any] | None:
    run_id, scen = split_rec_id(rec_id)
    df = db.query("SELECT RUN_ID, SCENARIO_ID, FORM_ID, OVERALL, NEEDS_SECOND_APPROVER, DRAFT_PR_JSON "
                  "FROM FF_AG_RECOMMENDATION WHERE RUN_ID = ? AND SCENARIO_ID = ?", (run_id, scen))
    return df.iloc[0].to_dict() if len(df) else None


def load_approvals(db: Db, rec_id: str) -> list[dict[str, Any]]:
    run_id, scen = split_rec_id(rec_id)
    df = db.query("SELECT LEVEL_NO, DECIDED_AT, APPROVER, ACTION, CHECKLIST_JSON, REASON, EDITED_QTY, "
                  "EDITED_SUPPLIER, SNOOZE_DAYS FROM FF_AG_APPROVAL WHERE RUN_ID = ? AND SCENARIO_ID = ? "
                  "ORDER BY DECIDED_AT", (run_id, scen))
    return df.to_dict(orient="records")


def latest_by_level(approvals: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for a in sorted(approvals, key=lambda a: _ts(a["DECIDED_AT"])):
        out[int(a["LEVEL_NO"])] = a
    return out


def _approving(a: dict[str, Any] | None) -> bool:
    if not a or a["ACTION"] not in (ApprovalAction.APPROVE.value, ApprovalAction.EDIT_APPROVE.value):
        return False
    try:
        return ApprovalChecklist.model_validate(json.loads(a["CHECKLIST_JSON"])).complete
    except (ValueError, TypeError):
        return False


def _num(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def edited_draft(rec: dict[str, Any], latest: dict[int, dict[str, Any]]) -> DraftPR:
    """Draft PR from A5 with the approvers' edits applied (level 2 overrides level 1)."""
    if not rec.get("DRAFT_PR_JSON"):
        raise ActRefused("recommendation has no draft PR (e.g. an internal rebalance); nothing to post")
    draft = DraftPR.model_validate(json.loads(rec["DRAFT_PR_JSON"]))
    upd: dict[str, Any] = {}
    for lvl in sorted(latest):
        a = latest[lvl]
        if _num(a.get("EDITED_QTY")):
            upd["quantity"] = _num(a["EDITED_QTY"])
        if a.get("EDITED_SUPPLIER"):
            upd["fixed_supplier"] = str(a["EDITED_SUPPLIER"])
    return draft.model_copy(update=upd)


def needs_second_approver(rec: dict[str, Any], draft: DraftPR | None, rules: Rules) -> bool:
    """A5's flag (above ₹ threshold or therapeutic substitute), re-evaluated on the edited PR value."""
    if bool(int(rec.get("NEEDS_SECOND_APPROVER") or 0)):
        return True
    if draft is None or draft.unit_price_inr is None:
        return False
    thr = float(rules.param("BUDGET", "second_approver_threshold_inr"))
    return draft.quantity * float(draft.unit_price_inr) > thr


def assert_approved(db: Db, rec_id: str, rules: Rules) -> tuple[dict[str, Any], DraftPR, dict[int, dict[str, Any]]]:
    """Raise ActRefused unless the recommendation may be acted on. Returns (rec, edited draft, latest approvals)."""
    rec = load_recommendation(db, rec_id)
    if rec is None:
        raise ActRefused(f"unknown recommendation {rec_id}")
    if rec["OVERALL"] == Overall.BLOCKED.value:
        raise ActRefused("recommendation is BLOCKED by A5; it never reaches the gate")
    latest = latest_by_level(load_approvals(db, rec_id))
    if not latest:
        raise ActRefused("no FF_AG_APPROVAL row: a human must approve first")
    if any(a["ACTION"] == ApprovalAction.REJECT.value for a in latest.values()):
        raise ActRefused("latest decision is REJECT")
    if not _approving(latest.get(1)):
        raise ActRefused("no level-1 approval with every checklist item ticked")
    draft = edited_draft(rec, latest)
    if needs_second_approver(rec, draft, rules):
        l2 = latest.get(2)
        if not _approving(l2):
            raise ActRefused("a second (level-2) approval is required (above ₹ threshold or therapeutic substitute)")
        if str(l2["APPROVER"]).strip().lower() == str(latest[1]["APPROVER"]).strip().lower():
            raise ActRefused("the level-2 approver must be a different person from level 1")
    return rec, draft, latest


# ---------------------------------------------------------------- S/4-shaped PR


def _dec(x: float | Decimal | None) -> str | None:
    """OData V2 Edm.Decimal travels as a string."""
    if x is None:
        return None
    d = Decimal(str(x)).normalize()
    return format(d, "f")


def s4_payload(draft: DraftPR, banfn: str | None = None, description: str | None = None) -> dict[str, Any]:
    """API_PURCHASEREQ_PROCESS_SRV deep-insert body: A_PurchaseRequisitionHeader + to_PurchaseReqnItem."""
    item = {
        "PurchaseRequisitionItem": str(PR_ITEM_NO),
        "Material": draft.material,
        "Plant": draft.plant,
        "RequestedQuantity": _dec(draft.quantity),
        "BaseUnit": draft.base_unit,
        "DeliveryDate": f"{draft.delivery_date.isoformat()}T00:00:00",
        "PurchasingGroup": draft.purchasing_group,
        "FixedSupplier": draft.fixed_supplier,
        "PurchaseRequisitionPrice": _dec(draft.unit_price_inr),
        "PurReqnItemCurrency": draft.currency,
    }
    header: dict[str, Any] = {"PurchaseRequisitionType": PR_DOC_TYPE}
    if banfn:
        header["PurchaseRequisition"] = banfn
    if description:
        header["PurReqnDescription"] = description[:40]
    header["to_PurchaseReqnItem"] = {"results": [{k: v for k, v in item.items() if v is not None}]}
    return header


def _next_banfn(db: Db) -> str:
    df = db.query("SELECT MAX(BANFN) AS B FROM FF_MM_EBAN WHERE BANFN LIKE ?", (f"{BANFN_PREFIX}%",))
    last = df.iloc[0]["B"] if len(df) else None
    n = int(str(last)[len(BANFN_PREFIX):]) + 1 if last and str(last)[len(BANFN_PREFIX):].isdigit() else 1
    return f"{BANFN_PREFIX}{n:08d}"


def load_action(db: Db, rec_id: str) -> dict[str, Any] | None:
    run_id, scen = split_rec_id(rec_id)
    df = db.query(f"SELECT {', '.join(ACTION_COLS)} FROM {ACTION_TABLE} WHERE RUN_ID = ? AND SCENARIO_ID = ?",
                  (run_id, scen))
    return df.iloc[0].to_dict() if len(df) else None


# ---------------------------------------------------------------- API Business Hub (read-only)


def check_master_data(material: str | None, supplier: str | None, api_key: str | None = None,
                      timeout: float = API_HUB_TIMEOUT_S) -> list[dict[str, Any]]:
    """GET A_Product / A_Supplier from the SAP API Business Hub sandbox. Never blocks: WARN on any problem."""
    key = api_key if api_key is not None else os.environ.get("SAP_API_HUB_KEY", "")
    lookups = [("API_PRODUCT_SRV", "A_Product", material), ("API_BUSINESS_PARTNER", "A_Supplier", supplier)]
    if not key:
        return [{"service": svc, "key": k, "status": CheckStatus.WARN.value, "note": "skipped: SAP_API_HUB_KEY not set"}
                for svc, _, k in lookups if k]
    out = []
    for svc, entity, k in lookups:
        if not k:
            continue
        odata_key = "'" + str(k).replace("'", "''") + "'"
        url = f"{API_HUB_BASE}/{svc}/{entity}({urllib.parse.quote(odata_key)})?$format=json"
        req = urllib.request.Request(url, method="GET", headers={"APIKey": key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ok = resp.status == 200
            out.append({"service": svc, "key": k, "status": (CheckStatus.PASS if ok else CheckStatus.WARN).value,
                        "note": "found in sandbox" if ok else f"HTTP {resp.status}"})
        except urllib.error.HTTPError as exc:
            note = "not found in sandbox (sandbox holds SAP demo data only)" if exc.code == 404 else f"HTTP {exc.code}"
            out.append({"service": svc, "key": k, "status": CheckStatus.WARN.value, "note": note})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            out.append({"service": svc, "key": k, "status": CheckStatus.WARN.value,
                        "note": f"skipped: no network ({type(exc).__name__})"})
    return out


# ---------------------------------------------------------------- act


@dataclass
class ActResult:
    rec_id: str
    banfn: str
    payload: dict[str, Any]
    payload_hash: str
    master_data: list[dict[str, Any]] = field(default_factory=list)
    audit_seq: int | None = None


def act(rec_id: str, db: Db, rules: Rules, check_master: bool = True, api_key: str | None = None) -> ActResult:
    """Post the approved PR to the FF_MM_EBAN mock. Raises ActRefused when not allowed."""
    run_id, scen = split_rec_id(rec_id)
    chain = verify_chain(run_id, db)
    if not chain.ok:
        raise ActRefused(f"audit {chain}")
    if load_action(db, rec_id) is not None:
        raise ActRefused(f"{rec_id} was already posted")
    rec, draft, latest = assert_approved(db, rec_id, rules)

    banfn = _next_banfn(db)
    payload = s4_payload(draft, banfn, description=f"FlowForge {rec['FORM_ID']}")
    md = check_master_data(draft.material, draft.fixed_supplier, api_key) if check_master else []
    for m in md:
        if m["status"] != CheckStatus.PASS.value:
            log.warning("[a6] WARN master data %s %s: %s", m["service"], m["key"], m["note"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date().isoformat()
    requester = str(latest[1]["APPROVER"])
    sqlite = db.backend == "sqlite"
    db.execute(f"INSERT INTO FF_MM_EBAN ({', '.join(EBAN_COLS)}) VALUES ({placeholders(EBAN_COLS)})", (
        banfn, PR_ITEM_NO, draft.material, draft.plant, draft.quantity, draft.base_unit,
        float(draft.unit_price_inr) if draft.unit_price_inr is not None else None, 1, today,
        draft.delivery_date.isoformat(), draft.fixed_supplier, requester[:40], EBAN_STATUS, run_id, scen,
        "FLOWFORGE_A6", f"mock:{S4_SERVICE}/{S4_ENTITY}", now.isoformat() if sqlite else now, "SYNTH"))
    p_json, p_hash = canonical(payload), sha256_hex(payload)
    db.execute(f"INSERT INTO {ACTION_TABLE} ({', '.join(ACTION_COLS)}) VALUES ({placeholders(ACTION_COLS)})", (
        run_id, scen, banfn, now.isoformat() if sqlite else now, requester, S4_SERVICE, p_json, p_hash,
        canonical(md)[:SOURCES_JSON_MAX]))
    ev = hop(db, run_id, AGENT, {"rec_id": rec_id, "approvals": latest, "draft_pr": draft},
             {"banfn": banfn, "payload": payload, "master_data": md},
             sources=[DataSourceRef(source=f"FF_MM_EBAN:mock:{S4_SERVICE}", fetched_at=now.replace(tzinfo=timezone.utc),
                                    is_proxy="SYNTH")],
             rule_versions=rule_file_versions(rules))
    return ActResult(rec_id, banfn, payload, p_hash, md, ev.seq)
