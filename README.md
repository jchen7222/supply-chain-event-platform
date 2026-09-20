# `manifest` — an event-sourced supply-chain platform over public data

**A manifest is a declaration of what shipped; every problem in this repo is a version of finding out whether the declaration was true.** When a pharma lot is recalled you need every box, every handoff, every holder — now, and provably: this repo is the record-keeping that makes that answer instant and audit-proof. It is the memory; its sibling repo (the Load Consolidation & Dispatch Planner) is the will — same append-only, bitemporal ledger pattern, applied there to dispatch decisions instead of movements. Public supply-chain data mutates in place, gets revised for years, and disagrees with itself across reporters — so the platform's thesis is an append-only, bitemporal event log from which any past state can be reconstructed and proven. The proof is in CI: the point-in-time fold is asserted, on every commit, to reproduce a vintage archive exactly.

**Honesty labels, up front.** The openFDA records (shortages, recalls, NDC directory), IMF trade series, and NY Fed GSCPI file in `fixtures/` are **real captures** (July–August 2026, sources cited below). Two things are not real and say so loudly: the ALFRED vintage fixture is **shaped per the FRED docs, not real archive data** (a free `FRED_API_KEY` activates the live fetch and the live version of the same CI assertion), and the pharma **movement stream is synthetic** — serialized movements are not public — generated (seeded) over **real NDC identifiers** with a **real recall** as the drill trigger. Nothing synthetic is presented as real.

## Quickstart

```bash
pip install -r requirements.txt
python -m pytest -q                                   # ingests fixtures, builds dbt, asserts 72
python -m ingest.run_snapshot --fixtures              # or run the pipeline directly
cd dbt && dbt build --profiles-dir profiles           # folds + marts + schema tests
```

## The envelope (modeled on GS1 EPCIS 2.0 / ISO-IEC 19987:2024)

```sql
create table raw.event_log (
    seq                   bigint,      -- append order; never reused
    event_id              varchar,     -- same non-null id may appear twice ONLY
                                       -- as an ErrorDeclaration (the EPCIS rule)
    event_type            varchar,     -- from the source registry
    source                varchar,
    entity_id             varchar,     -- natural key (series:date, recall_number, pair:year)
    event_time            varchar,     -- when it happened in the world      (EPCIS eventTime)
    record_time           varchar,     -- when THIS repository learned it    (EPCIS recordTime)
    payload               varchar,     -- the observation, as json
    payload_hash          varchar,
    is_error_declaration  boolean,     -- corrections rescind; they never modify
    declaration_time      varchar,
    reason                varchar,
    corrective_event_ids  varchar      -- pointers FORWARD to replacements
);
```

Four decisions taken directly from the standard: `eventTime` vs `recordTime` is EPCIS's bitemporal split; *"there is no mechanism … by which an application can delete or modify an EPCIS Event"* — corrections are subsequent events; an ErrorDeclaration reuses the original `event_id` (*"the sole case where the same non-null eventID may appear in two events"*) and points forward via `correctiveEventIDs`; and the repository assigns `record_time`. Scope, stated plainly: the envelope is **modeled on** EPCIS after reading the standard. It is not a certified implementation, and nothing here claims EPCIS production experience.

```
landing/ (raw bytes, append-only, content-addressed)     — disposable? NO: evidence
raw.event_log (the envelope)                             — PERMANENT: the system of record
─────────────────────────────────────────────────────── the line ───
dbt: staging → folds → marts                             — DISPOSABLE: rebuilt from the log at will
```

## The four folds (dbt), with real output

`current_state` and `point_in_time` are the **same macro** (`fold_state`) with a different `as_of` — that identity is the design. `history` chains versions with `lead()`; `aggregates` summarizes. From this build:

```
alfred    series_value_observed    events=8    entities=6
gscpi     index_value_observed     events=342  entities=342
imf_imts  trade_mirror_observed    events=15   entities=15
openfda   fda_shortage_observed    events=100  entities=100
openfda   fda_recall_observed      events=100  entities=100
```

**The CI assertion that anchors the repo** (`tests/test_dbt_folds.py::test_point_in_time_in_dbt_matches_vintage_archive`): rebuild `point_in_time` with `--vars {as_of: <vintage date>}` and assert it equals, entity for entity, what the vintage archive says the series looked like on that date — including a revision changing a value between vintages and an observation that must be absent before the vintage that introduced it. ALFRED matters because it is the **only** source here with a real vintage API; Census, BLS, and openFDA serve current state only. That asymmetry is the argument for the platform.

## Stage 0 — the API-integration layer (and saying so)

Three real public APIs, three different shapes — key-optional REST (FRED/ALFRED), rate-limited JSON with skip/limit pagination (openFDA), SDMX 3.0 (IMF) — plus a binary download (NY Fed), all integrated through **one client pattern** (`ingest/client.py`): retry with exponential backoff, rate-limit pacing, response caching, a pagination helper that never trusts the API to end pagination for it, and a **per-source circuit breaker** that fails fast once open. CI runs from frozen fixtures and never calls a live API (`tests/test_client.py` proves the client behaviors with fake transports); the scheduled ingest workflow exercises the live paths daily, so integration rot is caught by the badge, not by a reviewer.

## Lane 2 — openFDA: where this platform is the only memory

Neither `drug/shortages.json` nor `*/enforcement.json` has any history — no as-of, no vintage, no last-modified. Records mutate in place and the previous value is gone. So: snapshot on a visible schedule (`.github/workflows/ingest.yml`), hash each record, append an event only when the hash changes. After a few weeks this repo owns a public history that exists nowhere else.

**The finding, re-verified live on 2026-08-01:** openFDA's documentation states recall records *"will remain unchanged after published"* once classified. The data disagrees: device recall **Z-0522-2022** (report date 2022-02-02) now carries `termination_date` **2026-07-24** — four years after publication. Terminations recorded during 2026 alone: **133 drug, 172 device, 1,005 food** records. A state table overwrites that silently; the log catches it because the hash changed (`tests/test_openfda.py::test_hash_diff_catches_a_mutation` reproduces the exact pattern).

Traps confirmed in the live capture, now regression-tested: status is `Ongoing` (the FDA's own prose page spells it `On-Going`, which returns zero records); sibling endpoints use different date formats (`20151125` vs `07/20/2026` — both normalized to ISO at ingest, with every normalization logged into the payload); production enums arrive misspelled (`Avaliable`).

## The recall drill — chain of custody with two clocks

The drill trigger is real: enforcement record **D-1285-2020** (mixed-strength amphetamine bottles), which carries its real NDC links (0555-0775 / -0971 / -0972). The movements beneath it are synthetic and labeled. The drill answers the three recall questions from the folds — where is every lot now, how did each get there, and what did we *believe* mid-day:

```
RECALL DRILL [as of 2026-07-30 12:00] — D-1285-2020 (Terminated)
  lot:0555-0775:L01            held by DEPOT-B  | PLANT-1 -> DEPOT-B -> PH-02
  lot:0555-0971:L03            held by DEPOT-B  | PLANT-1 -> DEPOT-B -> PH-01
  ...
RECALL DRILL [current]
  lot:0555-0775:L01            held by PH-02    | PLANT-1 -> DEPOT-B -> PH-02
  lot:0555-0971:L03            held by PH-01    | PLANT-1 -> DEPOT-B -> PH-01
```

At noon the system believed the lots sat at depots; by close of day the receives had landed (one of them **recorded hours late on purpose** — the as-of fold shows belief, not hindsight, which is exactly what an auditor asks for). Read models: `lot_current_location`, `chain_of_custody`; drill runner in `ingest/movements.py`.

**Revisions are not corrections — the envelope distinguishes them.** An ALFRED revision or a GSCPI re-estimate is the *world* updating: a new event with a new `record_time`. A correction is *us* fixing an ingest error: an EPCIS-style ErrorDeclaration that rescinds by `event_id` and points forward to its replacements. Both are modeled, separately, and the as-of fold honors both (before a declaration was recorded, the bad value *was* the belief — `tests/test_envelope.py::test_point_in_time_is_as_of_aware`).

## Lane 3 — bilateral trade asymmetry (real IMF data)

Two countries report the same physical shipment; the numbers never match. From the captured IMTS series (free, no key — and note the renamed dataset: searching "DOTS" misleads):

```
2015: CAN says 54.3bn | CHN says 29.4bn | gap 24.9bn (45.9%) | valuation 3.1bn | attribution+ 21.8bn
2016: CAN says 51.5bn | CHN says 27.9bn | gap 23.6bn (45.9%) | valuation 2.9bn | attribution+ 20.7bn
2017: CAN says 57.8bn | CHN says 31.8bn | gap 26.0bn (45.1%) | valuation 3.3bn | attribution+ 22.8bn
2020: CAN says 60.3bn | CHN says 42.1bn | gap 18.2bn (30.1%) | valuation 3.4bn | attribution+ 14.7bn
```

The decomposition is the judgment call the README exists for: the CIF/FOB **valuation** component (computable inside IMTS because it carries both `MG_CIF_USD` and `MG_FOB_USD`) explains only ~3bn of a ~24bn gap — **partner attribution dominates**, exactly as the UN compilers manual and the OECD conclude (goods routed through entrepôts are attributed to different partners by each side). `tests/test_imf.py` asserts the decomposition adds back up and that attribution outweighs valuation; the `reconciliation_report` mart ranks pairs by **unexplained residual** — asymmetry is not error, and a reconciliation that "fixes" it has destroyed information. (A dbt singular test enforces the sum at build time.) Two traps are load-bearing: the legacy `dataservices.imf.org` endpoint is DNS-dead with its docs still online (use `api.imf.org/external/sdmx/3.0`), and an empty-string wildcard returns **HTTP 200 with zero series and no error** — this codebase raises `ZeroSeriesError` on that shape, loudly, because a silent empty result is the silent-truncation bug wearing SDMX clothing.

## The fourth source — GSCPI, the add-a-source proof

The NY Fed Global Supply Chain Pressure Index: one free file, 342 monthly observations back to 1998, re-estimated over its full history every release (inputs arrive with *"revisions to up to twelve months of previous data"* — the most extreme revision behavior here, which is why it belongs in an event log). Adding it touched one registry row and one ~60-line mapping module — **pin that commit** (see Publishing below). Trap, confirmed live: the download is an OLE2 legacy `.xls` wearing an `.xlsx` extension (magic bytes `d0cf11e0`); `openpyxl` fails on it, so `ingest/gscpi.py` sniffs the magic and picks the engine — regression-tested.

## Lane 5 — USITC HTS: the rule as reference data, and its own oracle

Every other lane answers *what was observed*. This one answers **what was the rule** — and a rule is where most systems quietly lose history, because a tariff table is normally a lookup that gets **overwritten** when rates change. Overwrite it once and every past landed cost silently re-prices at today's rates.

The USITC publishes the Harmonized Tariff Schedule as **~249 dated revisions back to 1989**, each downloadable as JSON or CSV, each citing the Federal Register notice behind it. That is a genuine vintage archive, so the same source supplies both halves: the **dimension** (duty rates as effective-dated rows) and the **assertion** (the archive read by an independent code path that never touches the event log).

```
hts: 10 duty-rate changes across 3 archived revisions
```

```sql
-- dbt build --select +duty_rate_as_of --vars '{as_of: "2024-06-01"}'
hts_code        general_rate_raw  ad_valorem_rate  effective_from
8507.60.00.20   3.4%              0.034            2024-05-15     -- lithium-ion
3304.99.50.00   Free              0.0              2024-05-15     -- cosmetics

--                              as_of "2026-09-01"
8507.60.00.20   7.5%              0.075            2025-07-01
3304.99.50.00   4.9%              0.049            2026-03-20
```

A lithium-ion shipment that left in **June 2024 owes $34 on $1,000** and must still owe $34 after two later revisions took the rate to 7.5% — asserted in Python and again through dbt (`test_a_shipment_is_not_repriced_by_a_later_revision`).

**The parse refuses rather than guesses.** The `general` column is a string, and only some of it is ad valorem:

| raw | parsed |
|---|---|
| `Free` | `0.0`, kind `free` |
| `3.4%` | `0.034`, kind `ad_valorem` |
| `3.3 cents/kg` | `None`, kind **`specific`** — needs a quantity |
| `16% + 2.5 cents/kg` | `None`, kind **`compound`** |

A specific duty cannot be multiplied by a declared value, so it is flagged, never coerced. `landed_duty()` raises on a `None` rate. Coercing would produce a plausible wrong number, which is worse than a refusal — and a dbt singular test fails the build if a rate and its kind ever disagree.

**What is deliberately *not* in the payload:** the revision label. Idempotency is a hash of the payload, so including the label would mark every code "changed" at every revision — 249 revisions × ~19,000 lines of no-ops instead of changes. The label is 1:1 with the effective date, which *is* on the event, so `revision_label()` recovers it and the fixture index carries the Federal Register citation. **Provenance that can be derived does not belong in the hashed fact.**

Fixtures here are shaped per the USITC JSON export and labelled as fixtures — three revisions with two real rate changes between them, not a copy of the archive.

## The four failure classes, reproduced in public data

1. **Records lost between services** — IMF's 200-with-zero-series wildcard (and UN Comtrade's silent 500-record preview cap). Caught by a row-count/emptiness assertion, not an exception handler.
2. **Silent upstream format change** — `Ongoing` vs `On-Going`, `Avaliable`, and two date formats on sibling FDA endpoints. Caught by normalization-with-logging at ingest.
3. **Late / out-of-order arrival** — a recall terminated four years after publication; a pressure index re-estimated back to 1998 monthly. Caught by the hash-diff against the log — the platform is the memory.
4. **Unit / valuation mismatch** — CIF vs FOB, at national scale, permanent, documented; decomposed rather than averaged away.

## Registry-driven generation

`dbt/models/generated/source_event_counts.sql` is written **by the registry seed at compile time** — a jinja loop over `seeds/registry.csv`. Adding a source adds its block with no model edit; `tests/test_dbt_folds.py` asserts the generated model always covers exactly the sources in the raw log.

## Tests (255 passing, 1 live-keyed skip)

Client layer: retry-then-success with exponential backoff; circuit opens and fails fast (zero further calls); response cache prevents repeat calls; pagination walks pages and stops. Envelope: append-only; same-`event_id`-only-via-ErrorDeclaration (also enforced as a dbt singular test); rescission is **as-of-aware**; **idempotent replay** — re-running the same fixtures appends zero events (exact-replay natural keys plus unchanged-observation hash checks), a test, not a hope. ALFRED: every vintage reproduced by the fold; late-added observation absent before its vintage; revision changes value between vintages; live variant when `FRED_API_KEY` is set. openFDA: ISO normalization + logging; hash-diff silence on no change; mutation caught. IMF: decomposition sums; attribution > valuation; `ZeroSeriesError` on the trap. GSCPI: magic sniff; full history; revision memory. Pharma: drill locates every lot; as-of shows belief (depot) vs fact (pharmacy); replay is a no-op. dbt: current-state uniqueness; history chain sanity; generated-model coverage; decomposition-sum and event-id-rule singular tests; **point-in-time == vintage archive, through dbt**. Order intake: a quote records the rate that made it, a rejected order carries its reason and no money, every order reaches exactly one state. Courier: the last scan is the latest by occurrence, a customs hold surfaces as needs-attention, a parcel with no order is reported. Spreadsheet intake: encodings, Chinese and English headers, Excel serial dates, and a re-drop that appends nothing. Price book: three refusals with distinct reasons, and replay idempotence keyed on the observation date rather than today.

## Portability

The dbt project runs on DuckDB, so the models build with no account and no
credentials — `pip install -r requirements.txt` and `dbt build` is the whole
setup. `dbt/profiles/profiles.yml` also carries an env-var-driven Snowflake
target (`dbt build -t snowflake`); the models are written in portable SQL and
the warehouse is a profile choice rather than a rewrite.

Live ALFRED ingestion needs a free FRED API key in `FRED_API_KEY`. Without one
the workflow warns and skips that source rather than failing, and the test
suite skips its live variant — every other assertion still runs.

## Licence

MIT — see [LICENSE](LICENSE). All data here is public (openFDA, IMF, NY Fed, ALFRED) or synthetic; nothing proprietary lives in this repository.
