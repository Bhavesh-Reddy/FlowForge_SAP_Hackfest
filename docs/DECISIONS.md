# Decisions (one line each: date · decision · why)

- 2026-09-26 · Scores are deterministic rules; the LLM only narrates · explainability, and consistency with our "human-approved, auditable" claim
- 2026-09-26 · Scope the MVP to 30–40 NLEM formulations · NPPA data needs hand verification; depth over breadth
- 2026-09-26 · Hospital MM data is synthetic and S/4-shaped (MARA/MCHB/MSEG/EKPO/EBAN) · no S/4 system access; makes a real swap mechanical
- 2026-09-26 · Human gate sits before A6's action; A6 also audits every hop · matches the pharmacist's approval workflow and control points
- 2026-09-26 · No Claude attribution in commits or PRs · team rule
- 2026-09-26 · GST on medicines stored as 5% (post Sep-2025 rate change), flagged verify · A5 price check uses ceiling + GST; confirm the rate per formulation
- 2026-09-26 · ApprovalDecision rejects APPROVE/EDIT_APPROVE unless all 6 checklist items are ticked; REJECT needs a reason · enforce the human gate in the contract, not only in the UI
- 2026-09-26 · Test fixtures load into SQLite as FF_FX_<FILE> tables via tests/conftest.py `fixture_db` · agent stages test offline without HANA
- 2026-09-27 · A1 with no API cost data returns band AMBER, headroom NaN and low confidence · unknown is not safe; never invent a number
- 2026-09-27 · A1 shock multiplies the whole API cost series (sustained level shift) · avoids a fake one-month slope in the trend
- 2026-09-27 · A1 reads plan §6 names (FF_REF_*); offline tests expose fixtures under those names via tests/fixtures/ref_views.sql · agents code against one schema
- 2026-09-27 · FF_G_E adds WEIGHT and IS_PROXY to the §6 (ID, SRC, DST, REL) shape · shares and data quality come from the graph alone, so the HANA and NetworkX paths agree
- 2026-09-27 · A2 uses HANA Graph only if SYS.GRAPH_WORKSPACES shows FF_SUPPLY_GRAPH valid, else NetworkX · Day-1 Graph result is still unknown
- 2026-09-27 · A manufacturer inherits its formulations' API origins in shares_upstream; origin counts only at ≥ graph.shared_origin_min_share · no manufacturer-level sourcing data
- 2026-09-27 · No known producer → HHI 1.0 and 1/n term 1.0 (max concentration) · unknown is not safe
