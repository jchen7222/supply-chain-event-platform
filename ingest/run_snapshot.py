"""Pipeline entry point.

  python -m ingest.run_snapshot --fixtures            # offline, reproducible
  python -m ingest.run_snapshot --live --source fda   # scheduled workflow path

Writes: data/landing/<source>/... (append-only raw), data/manifest.duckdb
(raw.event_log), data/event_log.jsonl, data/state.json (hash memory).

The output directory is overridable with MANIFEST_DATA. That exists so the
TEST SUITE can build a throwaway warehouse somewhere else instead of deleting
and rebuilding the repository's own data/ — those files are tracked, and
data/event_log.jsonl is the real collected history the nightly ingest appends
to. It pairs with MANIFEST_DB, which dbt/profiles/profiles.yml already reads,
so both halves of the pipeline can be pointed at the same temp directory."""
import argparse
import datetime as dt
import json
import os
import sys

import duckdb

from . import (alfred, decisions, gscpi, hts, imf, movements, openfda, orders,
               orders_csv, price_book)
from .envelope import EventLog
from .landing import land
from .registry import write_seed

# Customer names in this data are Chinese, and Windows still defaults stdout to
# a legacy code page (cp1252), which cannot encode them — so `print` raised
# UnicodeEncodeError, the process exited 1, and every test depending on the
# session fixture errored out. CI never caught it because CI is Linux, where
# the default is already UTF-8. Reconfigure both streams at import, so any
# entry point into this package is safe on any platform.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # not a TextIOWrapper, or already closed
        pass

DATA = (os.environ.get("MANIFEST_DATA")
        or os.path.join(os.path.dirname(__file__), "..", "data"))
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


def _arrival(scan):
    """When the webhook reached us. Normally an hour after the scan; the customs
    hold on YT8802 takes three days, which is the case the receipt time exists
    for."""
    import datetime as dt
    if scan.get("scan_id") == "s10":
        return "2026-09-06T08:00:00+00:00"
    raw = scan.get("occurred_at") or scan.get("scanned_at")
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return (t + dt.timedelta(hours=1)).isoformat()
    except (ValueError, TypeError):
        return dt.datetime.now(dt.timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--source", default="all",
                    choices=["all", "alfred", "fda", "hts", "imf", "gscpi", "pharma",
                             "decisions", "orders", "tracking"])
    ap.add_argument("--as-of", default=None,
                    help="record_time stamp for this snapshot (default: today UTC)")
    ap.add_argument("--orders-csv", default=None, metavar="PATH",
                    help="price a spreadsheet of orders instead of the JSONL "
                         "fixture (Chinese or English headers, UTF-8 or GB18030)")
    ap.add_argument("--results", default=None, metavar="PATH",
                    help="write the priced sheet here (with --orders-csv)")
    ap.add_argument("--day-first", action="store_true",
                    help="read ambiguous dates as D/M/Y instead of M/D/Y")
    ap.add_argument("--price-book", default="price_book.jsonl", metavar="NAME",
                    help="observed site prices, read as-of each order's date "
                         "(fixtures/<NAME>; 'none' to disable)")
    ap.add_argument("--decisions", default=None, metavar="PATH",
                    help="routing decisions exported by dispatch-planner "
                         "(default: the committed sample fixture)")
    args = ap.parse_args()
    if args.results and not args.orders_csv:
        ap.error("--results needs --orders-csv")
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

    if args.source in ("all", "hts"):
        # Effective-dated reference data: each archived HTS revision lands as a
        # set of duty_rate rows stamped with the revision's effective date, so
        # the rate applied to a past shipment is the rate that was in force on
        # its ship date. Live fetch pulls a named revision; the fixtures are
        # three doc-shaped revisions with two real rate changes between them.
        revisions = hts.load_revisions()
        n = hts.replay(log, revisions)
        print(f"hts: {n} duty-rate changes across {len(revisions)} archived revisions"
              + ("" if live else "  [doc-shaped fixtures — see ingest/hts.py]"))

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

    if args.source in ("all", "orders"):
        refusals = []
        if args.orders_csv:
            # The spreadsheet path. Every row of the file is accounted for
            # before anything is priced: rows = accepted + refused, printed, so
            # the person who uploaded it can see their own row count come back.
            with open(args.orders_csv, "rb") as f:
                recs, refusals, meta = orders_csv.parse_csv(
                    f.read(), day_first=args.day_first)
            land(os.path.join(DATA, "landing"), "orders_csv",
                 os.path.basename(args.orders_csv),
                 open(args.orders_csv, "rb").read(), today)
            print(f"orders: {os.path.basename(args.orders_csv)} "
                  f"[{meta['encoding']}, '{meta['delimiter']}'] "
                  f"{meta['rows']} rows -> {meta['accepted']} accepted, "
                  f"{meta['refused']} refused")
            if not meta["has_price_column"]:
                print("        no price column in this export: every order is "
                      "recorded as placed and left unpriced")
            if meta["unmapped_columns"]:
                print(f"        columns ignored: {meta['unmapped_columns']}")
            for d in meta["duplicate_refs"]:
                print(f"        DUPLICATE order_ref {d['order_ref']} x{d['count']} "
                      f"{d['customers']} — the later row amends the earlier one")
            for r in refusals:
                print(f"        refused line {r['line']}: {r['reason']}")
        else:
            recs = orders.load_fixture()

        # The source price, looked up as-of each order's own date. This runs
        # BEFORE pricing and never after: a quote must be computed from the
        # price that was in force when the order was placed, not the one on the
        # site now. Orders the book cannot price keep their gap reason and are
        # rejected with it, so they arrive as a work list naming exactly what
        # to go and observe.
        if args.price_book and args.price_book.lower() != "none":
            book_rows = price_book.load(args.price_book)
            if book_rows:
                n = price_book.replay(log, book_rows)
                book = price_book.index(book_rows)
                need = [o for o in recs if o.get("retail_price_cad") in (None, "")]
                recs = price_book.apply_to(recs, book)
                filled = [o for o in recs if o.get("price_observed_on")]
                print(f"prices: {len(book_rows)} observations "
                      f"({n} new to the ledger) -> {len(filled)} of "
                      f"{len(need)} orders priced from the book")
                for o in filled:
                    if o["price_match"] != "style_colour_size":
                        print(f"        {o['order_ref']}: matched by "
                              f"{o['price_match']}, not exactly — "
                              f"C${o['retail_price_cad']:.2f} observed "
                              f"{o['price_observed_on']}")
                for o in recs:
                    if o.get("price_gap"):
                        print(f"        LOOK UP {o['order_ref']} "
                              f"({o.get('style_no') or o.get('product_name')}): "
                              f"{o['price_gap']}")
            elif args.orders_csv:
                print(f"prices: fixtures/{args.price_book} is empty or missing — "
                      f"orders without a price column cannot be quoted")

        c = orders.replay(log, recs, record_time=today)
        print(f"orders: {c['placed']} placed, {c['quoted']} quoted, "
              f"{c['rejected']} rejected"
              + (f", {c['loss_making']} LOSS-MAKING" if c['loss_making'] else ""))
        if args.orders_csv and not any(c[k] for k in ("placed", "quoted", "rejected")):
            print("        nothing new: this file has been priced already, and "
                  "re-reading it appends nothing")
        for g in orders.unquotable(recs):
            print(f"        {g['order_ref']} ({g['customer']}): {g['reason']}")
        if args.results:
            rows = orders_csv.write_results(args.results, recs, refusals)
            print(f"        priced sheet -> {args.results} ({len(rows)} rows)")

    if args.source in ("all", "tracking"):
        # The AWS lane, run in process against moto-mocked AWS — the same
        # handler code a deployment calls, with no credentials. See tracking/.
        from tracking import projections as tpj
        from tracking.handler import as_sqs_event, consume
        import boto3
        from moto import mock_aws
        feed = json.load(open(os.path.join(FIX, "courier_scans_sample.json"),
                              encoding="utf-8"))
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="manifest-tracking-log")
            ev = as_sqs_event([{"scan": sc, "received_at": _arrival(sc)}
                               for sc in feed["good"] + feed["bad"]])
            counts = consume(ev, s3_client=s3, bucket="manifest-tracking-log",
                             run_id=today)
            scan_log = tpj.read_log(s3, "manifest-tracking-log")
            quarantined = tpj.read_quarantine(s3, "manifest-tracking-log")
        hop = tpj.reconcile(counts)
        n = tpj.to_events(scan_log, log, record_time=today)
        print(f"tracking: {counts['messages']} webhooks -> {counts['landed']} landed, "
              f"{counts['duplicates']} duplicate, {counts['quarantined']} quarantined "
              f"[{'RECONCILED' if hop['reconciled'] else 'MISMATCH'}] -> {n} events")
        for q in quarantined:
            print(f"          quarantined: {q['reason']}")
        for s_ in tpj.late_scans(scan_log, days=2):
            print(f"          late by {s_['lag_days']}d: {s_['waybill']} "
                  f"{s_['status']} (scanned {s_['occurred_at'][:10]})")

    if args.source in ("all", "decisions"):
        # The seam. dispatch-planner writes this file; we read it as a source.
        # Live mode has no endpoint to call — the planner is a peer system, not
        # an API — so the same file is read either way and we say so.
        #
        # --decisions exists so an orchestrator can point this at the file the
        # planner just produced, WITHOUT copying it over the committed sample.
        # That was the first thing I made the runner do, and it silently
        # overwrote a tracked fixture: the next test run then read a one-line
        # stub as the seam contract and the whole build failed somewhere else
        # entirely. A pipeline should read where it is told to read, not have
        # its own fixtures rewritten underneath it.
        path = args.decisions or os.path.join(FIX, "dispatch_decisions_sample.jsonl")
        recs = decisions.parse(open(path, "rb").read())
        land(os.path.join(DATA, "landing"), "dispatch_planner",
             "dispatch_decisions.jsonl", open(path, "rb").read(), today)
        n = decisions.replay(log, recs, record_time=today)
        gaps = decisions.unpriceable(recs)
        print(f"decisions: {n} routing decisions ingested from the planner "
              f"({len(recs)} in file, {len(gaps)} with no tariff heading)")
        for g in gaps:
            print(f"           {g['order_id']}: {g['why']} "
                  f"({g['commodity_class']}, rule {g['matched_rule']})")

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
