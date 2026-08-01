# `manifest` — an event-sourced supply-chain platform over public data

**A manifest is a declaration of what shipped; every problem in this repo is a version of finding out whether the declaration was true.** When a pharma lot is recalled you need every box, every handoff, every holder — now, and provably: this repo is the record-keeping that makes that answer instant and audit-proof. It is the memory; its sibling repo (the Load Consolidation & Dispatch Planner) is the will — same append-only, bitemporal ledger pattern, applied there to dispatch decisions instead of movements. Public supply-chain data mutates in place, gets revised for years, and disagrees with itself across reporters — so the platform's thesis is an append-only, bitemporal event log from which any past state can be reconstructed and proven. The proof is in CI: the point-in-time fold is asserted, on every commit, to reproduce a vintage archive exactly.

**Honesty labels, up front.** The openFDA records (shortages, recalls, NDC directory), IMF trade series, and NY Fed GSCPI file in `fixtures/` are **real captures** (July–August 2026, sources cited below). Two things are not real and say so loudly: the ALFRED vintage fixture is **shaped per the FRED docs, not real archive data** (a free `FRED_API_KEY` activates the live fetch and the live version of the same CI assertion), and the pharma **movement stream is synthetic** — serialized movements are not public — generated (seeded) over **real NDC identifiers** with a **real recall** as the drill trigger. Nothing synthetic is presented as real.

## Quickstart

```bash
pip install -r requirements.txt
python -m pytest -q                                   # ingest fixtures + dbt build + 20 assertions
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

Four decisions taken directly from the standard: `eventTime` vs `recordTime` is EPCIS's bitemporal split; *"there is no mechanism … by which an application can delete or modify an EPCIS Event"* — corrections are subsequent events; an ErrorDeclaration reuses the original `event_id` (*"the sole case where the same non-null eventID may appear in two events"*) and points forward via `correctiveEventIDs`; and the repository assigns `record_time`. Claim made here: the envelope is **modeled on** EPCIS after reading the standard — not EPCIS production experience.

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

## The four failure classes, reproduced in public data

1. **Records lost between services** — IMF's 200-with-zero-series wildcard (and UN Comtrade's silent 500-record preview cap). Caught by a row-count/emptiness assertion, not an exception handler.
2. **Silent upstream format change** — `Ongoing` vs `On-Going`, `Avaliable`, and two date formats on sibling FDA endpoints. Caught by normalization-with-logging at ingest.
3. **Late / out-of-order arrival** — a recall terminated four years after publication; a pressure index re-estimated back to 1998 monthly. Caught by the hash-diff against the log — the platform is the memory.
4. **Unit / valuation mismatch** — CIF vs FOB, at national scale, permanent, documented; decomposed rather than averaged away.

## Registry-driven generation

`dbt/models/generated/source_event_counts.sql` is written **by the registry seed at compile time** — a jinja loop over `seeds/registry.csv`. Adding a source adds its block with no model edit; `tests/test_dbt_folds.py` asserts the generated model always covers exactly the sources in the raw log.

## Tests (28 passing, 1 live-keyed skip)

Client layer: retry-then-success with exponential backoff; circuit opens and fails fast (zero further calls); response cache prevents repeat calls; pagination walks pages and stops. Envelope: append-only; same-`event_id`-only-via-ErrorDeclaration (also enforced as a dbt singular test); rescission is **as-of-aware**; **idempotent replay** — re-running the same fixtures appends zero events (exact-replay natural keys plus unchanged-observation hash checks), a test, not a hope. ALFRED: every vintage reproduced by the fold; late-added observation absent before its vintage; revision changes value between vintages; live variant when `FRED_API_KEY` is set. openFDA: ISO normalization + logging; hash-diff silence on no change; mutation caught. IMF: decomposition sums; attribution > valuation; `ZeroSeriesError` on the trap. GSCPI: magic sniff; full history; revision memory. Pharma: drill locates every lot; as-of shows belief (depot) vs fact (pharmacy); replay is a no-op. dbt: current-state uniqueness; history chain sanity; generated-model coverage; decomposition-sum and event-id-rule singular tests; **point-in-time == vintage archive, through dbt**.

## Publishing (staged, so the proofs are pinnable)

```bash
git init && git add -A ':!ingest/gscpi.py' ':!fixtures/gscpi_data.xlsx'
git commit -m "manifest: EPCIS-modeled envelope, ALFRED replay, openFDA memory, IMF mirror reconciliation"
git add ingest/gscpi.py fixtures/gscpi_data.xlsx
git commit -m "add GSCPI: one registry row + one mapping module"   # <- the pinned add-a-source commit
gh repo create manifest --public --source=. --push
# then: repo Settings -> Secrets -> FRED_API_KEY (free key) to activate live ALFRED
```

Snowflake note: the dbt project runs on DuckDB so a reviewer needs no account, and `dbt/profiles/profiles.yml` now carries a second, env-var-driven **Snowflake target** (`dbt build -t snowflake`) — the dual target is the portability talking point. Claim Snowflake on the resume line only after you have actually run the models on a trial account.
