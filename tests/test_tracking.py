"""The AWS tracking lane: courier webhook → API Gateway → SQS → Lambda → S3.

Everything runs the REAL handlers against moto-mocked AWS, in process, with no
credentials — the same code path a deployment calls.

The fixture carries every awkward case exactly once: a scan we hear about three
days late, a delivery webhook that wins the race against customs clearance, a
correction resent under the same scan id, two live schema versions, and one
reject per rejection reason.
"""
from __future__ import annotations

import json
import os

import boto3
import pytest
from moto import mock_aws

from tracking import projections as pj
from tracking.envelope import ScanError, canonical_status, content_key, validate
from tracking.handler import as_sqs_event, consume, receive

BUCKET = "manifest-tracking-log"
QUEUE = "tracking-scans"
REGION = "us-east-1"
FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "courier_scans_sample.json")


@pytest.fixture(scope="module")
def feed():
    with open(FIX, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def aws():
    """A mocked bucket and queue, plus the real handlers wired to them."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
        yield {"s3": s3, "sqs": sqs, "queue": url}


def _post(aws, scans, carrier="YunExpress"):
    return receive({"body": json.dumps(scans), "headers": {"x-carrier": carrier}},
                   sqs_client=aws["sqs"], queue_url=aws["queue"])


def _drain(aws):
    """Pull the queue and shape it exactly as Lambda receives it."""
    msgs = []
    while True:
        r = aws["sqs"].receive_message(QueueUrl=aws["queue"],
                                       MaxNumberOfMessages=10)
        got = r.get("Messages", [])
        if not got:
            break
        for m in got:
            msgs.append(json.loads(m["Body"]))
            aws["sqs"].delete_message(QueueUrl=aws["queue"],
                                      ReceiptHandle=m["ReceiptHandle"])
    return as_sqs_event(msgs)


# Webhooks normally arrive within the hour of the scan. One does not: W8802's
# customs hold takes three days to reach us, which is the case the whole
# receipt-time mechanism exists for. Modelling arrival here rather than letting
# every scan default to "now" is what makes the as-of tests mean anything.
LATE_ARRIVALS = {"s10": "2026-09-06T08:00:00+00:00"}


def _with_arrivals(ev):
    """Stamp each queued message with a realistic receipt time: one hour after
    the scan happened, unless LATE_ARRIVALS says otherwise."""
    import datetime as dt
    for rec in ev["Records"]:
        body = json.loads(rec["body"])
        sid = body["scan"].get("scan_id")
        occurred = body["scan"].get("occurred_at") or body["scan"].get("scanned_at")
        if sid in LATE_ARRIVALS:
            body["received_at"] = LATE_ARRIVALS[sid]
        elif occurred:
            try:
                t = dt.datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
                body["received_at"] = (t + dt.timedelta(hours=1)).isoformat()
            except ValueError:
                pass                    # a bad timestamp keeps the real clock
        rec["body"] = json.dumps(body)
    return ev


def _ingest(aws, scans, run_id="r1"):
    _post(aws, scans)
    return consume(_with_arrivals(_drain(aws)), s3_client=aws["s3"],
                   bucket=BUCKET, run_id=run_id)


# ── the envelope ─────────────────────────────────────────────────────────────

def test_chinese_and_english_and_numeric_statuses_all_canonicalise():
    assert canonical_status("已签收") == "delivered"
    assert canonical_status("Held by customs") == "customs_held"
    assert canonical_status("45") == "customs_cleared"


def test_an_unmapped_customs_phrase_is_refused_not_guessed():
    """A mis-mapped customs status is the difference between 'arriving Tuesday'
    and 'we need another document from you'. Guessing is the bug."""
    with pytest.raises(ScanError) as e:
        canonical_status("宇宙传送中")
    assert "unmapped" in str(e.value)


def test_a_legacy_v1_scan_upgrades_at_the_edge(feed):
    v1 = next(s for s in feed["good"] if s["schema"] == 1)
    out = validate(v1)
    assert out["schema"] == 2 and out["upgraded_from"] == 1
    assert out["status"] in ("accepted", "in_transit", "export_customs")
    assert out["country"] is None, "v1 carries no country — not assumed"


def test_timestamps_normalise_to_utc():
    s = validate({"schema": 2, "scan_id": "x", "waybill": "YT1234567",
                  "status_code": "10", "occurred_at": "2026-09-01T10:00:00+08:00"})
    assert s["occurred_at"] == "2026-09-01T02:00:00+00:00"


def test_dedupe_key_is_content_not_the_carriers_id_alone():
    """Carriers reuse scan ids and resend them with corrected statuses. Keying
    on the id alone would swallow the correction."""
    a = validate({"schema": 2, "scan_id": "s18", "waybill": "DH550002",
                  "status_code": "47", "occurred_at": "2026-09-05T07:00:00Z"})
    b = validate({"schema": 2, "scan_id": "s18", "waybill": "DH550002",
                  "status_code": "45", "occurred_at": "2026-09-05T07:00:00Z"})
    assert content_key(a) != content_key(b)


# ── API Gateway: accept fast, judge later ────────────────────────────────────

def test_a_carrier_gets_202_even_for_a_scan_we_cannot_parse(aws, feed):
    """A 4xx makes the carrier retry and eventually disable the webhook, so
    rejection happens in the consumer where it can be quarantined."""
    r = _post(aws, feed["bad"])
    assert r["statusCode"] == 202
    assert json.loads(r["body"])["queued"] == len(feed["bad"])


def test_a_body_that_is_not_json_is_the_only_400(aws):
    r = receive({"body": "<html>oops</html>"}, sqs_client=aws["sqs"],
                queue_url=aws["queue"])
    assert r["statusCode"] == 400


def test_a_single_object_and_a_list_both_work(aws, feed):
    assert json.loads(_post(aws, feed["good"][0])["body"])["queued"] == 1
    assert json.loads(_post(aws, feed["good"][:3])["body"])["queued"] == 3


# ── the hop that must reconcile ──────────────────────────────────────────────

def test_messages_equal_landed_plus_duplicates_plus_quarantined(aws, feed):
    """The lane's conservation property. If this fails a scan was lost between
    the queue and the log, and a customer is being told something the log
    cannot support."""
    counts = _ingest(aws, feed["good"] + feed["bad"])
    r = pj.reconcile(counts)
    assert r["reconciled"], r
    assert counts["quarantined"] == len(feed["bad"])
    assert counts["landed"] > 0


def test_every_reject_lands_in_quarantine_with_a_reason(aws, feed):
    _ingest(aws, feed["bad"])
    q = pj.read_quarantine(aws["s3"], BUCKET)
    assert len(q) == len(feed["bad"])
    reasons = " ".join(r["reason"] for r in q)
    for expected in ("unmapped carrier status", "unknown schema",
                     "missing required fields", "implausible waybill",
                     "not an ISO timestamp"):
        assert expected in reasons, expected


def test_at_least_once_delivery_is_idempotent(aws, feed):
    """SQS is at-least-once by design, so dedupe is not an optimisation here —
    it is what makes the lane correct."""
    first = _ingest(aws, feed["good"], run_id="r1")
    second = _ingest(aws, feed["good"], run_id="r2")
    assert first["landed"] > 0
    assert second["landed"] == 0
    assert second["duplicates"] == first["landed"]


def test_a_repeat_inside_one_batch_is_also_collapsed(aws, feed):
    counts = _ingest(aws, feed["good"][:3] + feed["good"][:3])
    assert counts["landed"] == 3 and counts["duplicates"] == 3


def test_the_log_is_partitioned_by_when_the_scan_HAPPENED(aws, feed):
    """Not by arrival. A late scan lands in its own day's partition, which is
    what makes an as-of fold a bounded read instead of a full scan."""
    _ingest(aws, feed["good"])
    keys = [o["Key"] for o in aws["s3"].list_objects_v2(
        Bucket=BUCKET, Prefix="scans/")["Contents"]]
    assert any("occurred_date=2026-09-01/" in k for k in keys)
    assert all(k.startswith("scans/occurred_date=") for k in keys)


# ── the three questions ──────────────────────────────────────────────────────

def test_out_of_order_scans_fold_by_occurrence_not_arrival(aws, feed):
    """W3's delivery webhook arrives before its customs-clearance scan. Sorting
    by arrival would show the parcel delivered and then still in customs."""
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    assert pj.where_is_it(log, "YT8803") == "delivered"
    seq = [s["status"] for s in pj.history(log, "YT8803")]
    assert seq == ["accepted", "customs_cleared", "delivered"]


def test_where_it_was_versus_what_we_said(aws, feed):
    """THE QUESTION THE RECEIPT TIME EXISTS FOR. W8802 was held at customs on
    the 3rd, but the webhook did not arrive until we ingested it. On the 3rd
    the parcel was already held; what the tracking page could say depends on
    what had been received."""
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    actually = pj.where_was_it(log, "YT8802", "2026-09-03")
    assert actually == "customs_held", "the scan had happened by the 3rd"
    said = pj.what_did_we_say(log, "YT8802", "2026-09-02")
    assert said == "in_transit", "on the 2nd we had not yet heard about the hold"


def test_a_correction_under_the_same_scan_id_wins(aws, feed):
    """The depot coded it held, then corrected it to cleared. Both are in the
    log; the fold shows the correction because it arrived later for the same
    moment."""
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    rows = [s for s in log if s["waybill"] == "DH550002"
            and s["occurred_at"].startswith("2026-09-05")]
    assert len(rows) == 2, "both the error and the correction are kept"
    assert pj.where_is_it(log, "DH550002") == "customs_cleared"


def test_late_scans_are_measurable(aws, feed):
    """A customs hold learned three days late is three days of a customer
    wondering — so it is a metric, not a footnote."""
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    late = pj.late_scans(log, days=3)
    assert [s["waybill"] for s in late] == ["YT8802"]
    assert late[0]["lag_days"] == 3
    assert late[0]["status"] == "customs_held"


def test_a_same_day_webhook_is_not_counted_as_late(aws, feed):
    """The metric has to stay quiet about normal traffic or nobody reads it."""
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    assert pj.late_scans(log, days=1) == pj.late_scans(log, days=3)


def test_stuck_parcels_are_a_work_list_not_an_error(aws, feed):
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    rows = pj.stuck(log, as_of_date="2026-09-07", days=5)
    assert [r["waybill"] for r in rows] == ["YT8806"]
    assert rows[0]["status"] == "in_transit"
    assert rows[0]["age_days"] >= 5


def test_a_delivered_parcel_is_never_stuck(aws, feed):
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    stuck = {r["waybill"] for r in pj.stuck(log, "2026-12-31", days=1)}
    assert "YT8801" not in stuck and "YT8803" not in stuck


# ── into the main ledger ─────────────────────────────────────────────────────

def test_scans_land_in_the_event_ledger_with_the_same_bitemporal_split(aws, feed):
    from ingest.envelope import EventLog
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    el = EventLog()
    n = pj.to_events(log, el, record_time="2026-09-15")
    assert n > 0
    ev = next(e for e in el.events if e["entity_id"] == "WAYBILL:YT8801")
    assert ev["event_type"] == "shipment_scanned"
    assert ev["record_time"] == "2026-09-15"
    assert ev["event_time"].startswith("2026-09-")


def test_reingesting_the_scan_log_appends_nothing(aws, feed):
    from ingest.envelope import EventLog
    _ingest(aws, feed["good"])
    log = pj.read_log(aws["s3"], BUCKET)
    el = EventLog()
    first = pj.to_events(log, el, record_time="2026-09-15")
    second = pj.to_events(log, el, record_time="2026-09-15")
    assert first > 0 and second == 0
