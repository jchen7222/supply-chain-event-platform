"""Pipeline entry point.

  python -m ingest.run_snapshot --fixtures            # offline, reproducible
  python -m ingest.run_snapshot --live --source fda   # scheduled workflow path

Writes: data/landing/<source>/... (append-only raw), data/manifest.duckdb
(raw.event_log), data/event_log.jsonl, data/state.json (hash memory)."""
import argparse
import datetime as dt
import json
import os

import duckdb

from . import alfred, gscpi, imf, movements, openfda
from .envelope import EventLog
from .landing import land
from .registry import write_seed

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures")

PAIR_FILES = {  # captured live from api.imf.org (July 2026) — real data
    "CAN_from_CHN": ("imf_can_m_chn.json", "imf_can_mfob_chn.json", "imf_chn_x_can.json"),
}
PAIR_KEYS = {
    "CAN_from_CHN": ("CAN.MG_CIF_USD.CHN.A", "CAN.MG_FOB_USD.CHN.A", "CHN.XG_FOB_USD.CAN.A"),
}


def _state():
    p = os.path.join(DATA, "state.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def _save_state(s):
    json.dump(s, open(os.path.join(DATA, "state.json"), "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--source", default="all",
                    choices=["all", "alfred", "fda", "imf", "gscpi", "pharma"])
    ap.add_argument("--as-of", default=None,
                    help="record_time stamp for this snapshot (default: today UTC)")
    args = ap.parse_args()
    live = args.live and not args.fixtures
    today = args.as_of or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    os.makedirs(os.path.join(DATA, "landing"), exist_ok=True)
    # append-only across runs: reload the full log, new events extend it
    log = EventLog.from_jsonl(os.path.join(DATA, "event_log.jsonl"))
    base = len(log.events)
    state = _state()

    if args.source in ("all", "fda"):
        if live:
            raw_s = raw_e = None
        else:
            raw_s = open(os.path.join(FIX, "openfda_shortages.json"), "rb").read()
            raw_e = open(os.path.join(FIX, "openfda_enforcement.json"), "rb").read()
        if raw_s:
            land(os.path.join(DATA, "landing"), "openfda", "shortages.json", raw_s, today)
            land(os.path.join(DATA, "landing"), "openfda", "enforcement.json", raw_e, today)
        state["openfda"] = openfda.snapshot(
            log, today, raw_shortages=raw_s, raw_enforcement=raw_e,
            prior_hashes=state.get("openfda"))

    if args.source in ("all", "alfred"):
        # alfred.api_key() strips whitespace, so a key that is only spaces reads
        # as absent and falls back to the fixture instead of crashing mid-request
        payload = (alfred.fetch_vintages_live() if live and alfred.api_key()
                   else json.load(open(os.path.join(FIX, "alfred_mnfctrirsa_vintages.json"))))
        n, vdates = alfred.replay(log, payload)
        print(f"alfred: {n} vintage-replay events across {len(vdates)} vintages"
              + ("" if live else "  [doc-shaped fixture — set FRED_API_KEY for live]"))

    if args.source in ("all", "imf"):
        for pair, (f_cif, f_fob, f_x) in PAIR_FILES.items():
            k_cif, k_fob, k_x = PAIR_KEYS[pair]
            if live:
                b_cif, b_fob, b_x = (imf.fetch_series(k) for k in (k_cif, k_fob, k_x))
            else:
                b_cif = open(os.path.join(FIX, f_cif), "rb").read()
                b_fob = open(os.path.join(FIX, f_fob), "rb").read()
                b_x = open(os.path.join(FIX, f_x), "rb").read()
            n = imf.asymmetry_events(
                log, today,
                imf.parse_series(b_cif, k_cif),
                imf.parse_series(b_fob, k_fob),
                imf.parse_series(b_x, k_x), pair)
            print(f"imf: {pair}: {n} mirror observations reconciled")

    if args.source in ("all", "gscpi"):
        raw = gscpi.fetch_live() if live else open(os.path.join(FIX, "gscpi_data.xlsx"), "rb").read()
        land(os.path.join(DATA, "landing"), "gscpi", "gscpi_data.xlsx", raw, today)
        n, vals = gscpi.snapshot(log, today, raw, prior_values=state.get("gscpi"))
        state["gscpi"] = vals
        print(f"gscpi: {n} new/changed monthly values (of {len(vals)} in the file)")

    if args.source in ("all", "pharma"):
        recall = movements.pick_recall(os.path.join(FIX, "openfda_enforcement.json"))
        n = movements.generate(log, recall["ndcs"], seed=7)
        print(f"pharma: {n} synthetic EPCIS-shaped movement events over real NDCs "
              f"(recall trigger {recall['recall_number']} is real)")

    con = duckdb.connect(os.path.join(DATA, "manifest.duckdb"))
    log.to_duckdb(con)
    con.close()
    log.to_jsonl(os.path.join(DATA, "event_log.jsonl"))
    write_seed(os.path.join(os.path.dirname(__file__), "..", "dbt", "seeds", "registry.csv"))
    _save_state(state)
    print(f"event_log: {len(log.events)} events ({len(log.events) - base} new this run) -> data/manifest.duckdb (raw.event_log)")


if __name__ == "__main__":
    main()
