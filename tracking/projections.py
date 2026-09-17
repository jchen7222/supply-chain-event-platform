"""Folding the S3 scan log into shipment state — current, and as-of a date.

THE TWO QUESTIONS, AND WHY THEY DIFFER. A scan that happened on Tuesday can
reach the webhook on Friday. So:

    where_is_it("W123")                  -> everything we know now
    where_was_it("W123", "Wednesday")    -> where it ACTUALLY was on Wednesday
    what_did_we_say("W123", "Wednesday") -> what the tracking page SAID that day

The third is the one customer service needs, and it is the one a system
without a receipt time cannot answer. It is also the one that settles an
argument: if the customer was told "in transit" on Wednesday, that was true of
our knowledge on Wednesday even though the parcel was already held at customs.

ORDERING IS BY occurred_at, NEVER BY ARRIVAL. Scans arrive out of order, so the
latest scan is the one with the newest `occurred_at` among those we had
received by the as-of moment. Sorting by arrival would show "delivered" before
"customs cleared" whenever the delivery webhook won the race.
"""
from __future__ import annotations

import json

from .envelope import is_terminal

LOG_PREFIX = "scans"
QUARANTINE_PREFIX = "quarantine"


def read_log(s3, bucket, prefix=LOG_PREFIX):
    """Every scan in the log. Small enough to read whole here; the key layout
    (`occurred_date=` partitions) is what makes a date-bounded read cheap in a
    real deployment."""
    out = []
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix + "/")
    for page in pages:
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            out.append(json.loads(body))
    return out


def read_quarantine(s3, bucket):
    return read_log(s3, bucket, prefix=QUARANTINE_PREFIX)


def _visible(scans, as_of_received=None):
    if as_of_received is None:
        return list(scans)
    return [s for s in scans if s["received_at"] <= as_of_received]


def _latest(scans):
    """Latest by when it happened, tie-broken by when we heard — so a
    correction that arrives later for the same moment wins."""
    return max(scans, key=lambda s: (s["occurred_at"], s["received_at"]))


def current_state(scans, as_of_received=None):
    """waybill -> the latest scan, folded. `as_of_received` reproduces what the
    platform knew at a past moment."""
    by_waybill = {}
    for s in _visible(scans, as_of_received):
        by_waybill.setdefault(s["waybill"], []).append(s)
    return {w: _latest(v) for w, v in by_waybill.items()}


def history(scans, waybill, as_of_received=None):
    """Every scan for one parcel, in the order events happened."""
    rows = [s for s in _visible(scans, as_of_received) if s["waybill"] == waybill]
    return sorted(rows, key=lambda s: (s["occurred_at"], s["received_at"]))


def where_is_it(scans, waybill):
    st = current_state(scans).get(waybill)
    return None if st is None else st["status"]


def where_was_it(scans, waybill, on_date):
    """Where the parcel ACTUALLY was on a date — every scan that had happened
    by then, regardless of when the webhook arrived."""
    rows = [s for s in scans
            if s["waybill"] == waybill and s["occurred_at"][:10] <= on_date]
    return None if not rows else _latest(rows)["status"]


def what_did_we_say(scans, waybill, on_date):
    """What the tracking page said on a date — only scans we had RECEIVED by
    the end of that day. This is the one that needs the receipt time."""
    rows = [s for s in scans
            if s["waybill"] == waybill
            and s["received_at"][:10] <= on_date
            and s["occurred_at"][:10] <= on_date]
    return None if not rows else _latest(rows)["status"]


def late_scans(scans, days=1):
    """Scans whose news was already stale when it arrived. The operational
    metric that matters: a customs hold we learn about three days late is three
    days of a customer wondering."""
    out = []
    for s in scans:
        lag = (s["received_at"][:10], s["occurred_at"][:10])
        if lag[0] > lag[1]:
            delta = _daydiff(s["occurred_at"][:10], s["received_at"][:10])
            if delta >= days:
                out.append(dict(s, lag_days=delta))
    return sorted(out, key=lambda s: -s["lag_days"])


def stuck(scans, as_of_date, days=5):
    """Parcels whose last scan is older than `days` and not terminal — the
    exception queue. Nothing here is an error; it is the work list."""
    out = []
    for waybill, st in current_state(scans).items():
        if is_terminal(st["status"]):
            continue
        age = _daydiff(st["occurred_at"][:10], as_of_date)
        if age >= days:
            out.append({"waybill": waybill, "status": st["status"],
                        "last_scan": st["occurred_at"][:10], "age_days": age,
                        "site": st.get("site")})
    return sorted(out, key=lambda r: -r["age_days"])


def reconcile(counts):
    """The hop: messages received = landed + duplicates + quarantined."""
    accounted = counts["landed"] + counts["duplicates"] + counts["quarantined"]
    return {"messages": counts["messages"], "accounted": accounted,
            "reconciled": counts["messages"] == accounted}


def _daydiff(a, b):
    import datetime as dt
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def to_events(log, event_log, record_time):
    """Land the folded scans in the main event ledger, so tracking joins the
    orders and the routing decisions in one place.

    One event per scan, entity `WAYBILL:<id>`. event_time is when the scan
    happened; record_time is this ingest — the same split the S3 log already
    carries, now in the warehouse.
    """
    n = 0
    for s in sorted(log, key=lambda s: (s["occurred_at"], s["received_at"])):
        payload = {k: s.get(k) for k in
                   ("waybill", "scan_id", "status", "status_raw", "site",
                    "country", "carrier", "received_at")}
        if event_log.append("shipment_scanned", "courier_tracking",
                            f"WAYBILL:{s['waybill']}", payload,
                            event_time=s["occurred_at"][:10],
                            record_time=record_time):
            n += 1
    return n
