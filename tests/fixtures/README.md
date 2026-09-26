# Test fixtures (all rows SYNTH)

Tiny CSV set for offline tests (plan §13.0). Load with the `fixture_db` pytest fixture in `tests/conftest.py`
(one table per file, named `FF_FX_<FILE>` e.g. `FF_FX_FORMULATIONS`).

| File | Rows | Shape |
|---|---|---|
| formulations.csv | 3 | ceiling price (excl. GST), API, NLEM, VED |
| bom_assumption.csv | 3 | API g/unit, yield, conversion/packaging/freight cost |
| apis.csv | 2 | HS code, top origin country + share |
| producers.csv | 8 links, 4 producers | formulation → manufacturer |
| api_cost_monthly.csv | 2 × 12 months | A01 rising (headroom eroding), A02 flat |
| mara.csv / mchb.csv / mseg.csv | 1 material | S/4-shaped master, batch stock (one expired, one quarantined), weekly goods issues (BWART 201) |

Every row carries SOURCE / SOURCE_URL / FETCHED_AT / IS_PROXY=SYNTH. Not real market data.
