"""The AWS lane: courier webhook → API Gateway → SQS → Lambda → S3 log.

WHY SQS AND NOT KINESIS. The telemetry side of this architecture buffers meter
readings on Kinesis, which is right for a high-volume ordered stream from
devices you control. Courier tracking is the opposite shape: individual HTTPS
POSTs from carriers you do not control, arriving in bursts, retried on any
non-2xx, and occasionally replayed hours later. SQS fits that — a queue with
at-least-once delivery, a visibility timeout, and a dead-letter queue for
poison messages.

The consequence is the design constraint: **at-least-once means the consumer
must be idempotent**, so deduplication is not an optimisation here, it is the
thing that makes the lane correct. tracking/envelope.py:content_key is where
that lives.

TWO HANDLERS, TWO JOBS.

  `receive` is the API Gateway integration. It does as little as possible:
  authenticate, validate that the body is JSON, put it on the queue, return
  202. It deliberately does NOT parse the scan — a carrier whose schema we do
  not recognise must still get a 202, or it will retry forever and eventually
  disable the webhook. Rejection happens later, in the queue consumer, where
  it can be quarantined and looked at.

  `consume` is the SQS-triggered Lambda. It validates, deduplicates, writes
  each accepted scan to the append-only S3 log under a key derived from its
  content, and writes each rejected one to `quarantine/` with the reason. It
  returns hop counts so the caller can assert the batch reconciles.

RUNNABLE WITH NO CREDENTIALS. The tests and the demo run this same handler
code against moto-mocked AWS, in process. infra/tracking.yaml documents the
deployed shape.

THE HOP THAT MUST RECONCILE:

    messages received = landed + duplicates + quarantined

If that does not hold, a scan was lost between the queue and the log, and a
customer is being told something the log cannot support.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from .envelope import ScanError, content_key, validate

LOG_PREFIX = "scans"
QUARANTINE_PREFIX = "quarantine"


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── API Gateway: accept fast, judge later ────────────────────────────────────

def receive(event, context=None, *, sqs_client=None, queue_url=None):
    """API Gateway proxy integration. Returns an API Gateway response dict.

    202 on anything that is syntactically JSON, because a carrier that gets a
    4xx retries and then disables the webhook. The only 400 is a body we cannot
    parse at all — there is nothing to queue and nothing to quarantine.
    """
    if sqs_client is None:
        import boto3
        sqs_client = boto3.client("sqs")
    queue_url = queue_url or os.environ.get("TRACKING_QUEUE_URL")

    body = event.get("body") or ""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400,
                "body": json.dumps({"error": "body is not JSON"})}

    scans = payload if isinstance(payload, list) else [payload]
    received_at = _now()
    queued = 0
    for scan in scans:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"scan": scan, "received_at": received_at,
                                    "carrier_hint": (event.get("headers") or {})
                                    .get("x-carrier")}))
        queued += 1
    return {"statusCode": 202,
            "body": json.dumps({"queued": queued, "received_at": received_at})}


# ── SQS consumer: the append-only log ────────────────────────────────────────

def consume(event, context=None, *, s3_client=None, bucket=None, run_id=None):
    """SQS-triggered Lambda. Returns hop counts for reconciliation."""
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3")
    bucket = bucket or os.environ["TRACKING_BUCKET"]
    run_id = run_id or (getattr(context, "aws_request_id", None)
                        or uuid.uuid4().hex[:12])

    counts = {"messages": 0, "landed": 0, "duplicates": 0, "quarantined": 0}
    seen_this_batch = set()

    for record in event.get("Records", []):
        counts["messages"] += 1
        try:
            envelope = json.loads(record["body"])
            raw_scan = envelope["scan"]
            received_at = envelope.get("received_at") or _now()
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            _quarantine(s3_client, bucket, run_id, record.get("body"),
                        f"unreadable queue message: {e}", _now())
            counts["quarantined"] += 1
            continue

        try:
            scan = validate(raw_scan)
        except ScanError as e:
            # The carrier already got its 202. This is where the judgment
            # happens, and the reason is kept so somebody can fix the mapping.
            _quarantine(s3_client, bucket, run_id, raw_scan, str(e), received_at)
            counts["quarantined"] += 1
            continue

        key = content_key(scan)
        # Two dedupe layers, because at-least-once delivery can repeat a message
        # inside one batch as well as across batches.
        if key in seen_this_batch or _exists(s3_client, bucket, _log_key(scan, key)):
            counts["duplicates"] += 1
            continue
        seen_this_batch.add(key)

        event_body = dict(scan, received_at=received_at,
                          pipeline_run_id=run_id, content_key=key)
        s3_client.put_object(Bucket=bucket, Key=_log_key(scan, key),
                             Body=json.dumps(event_body, sort_keys=True).encode(),
                             ContentType="application/json")
        counts["landed"] += 1

    return counts


def _log_key(scan, key):
    """Content-addressed, partitioned by the day the scan HAPPENED — not the
    day it arrived. A late scan therefore lands in its own day's partition,
    which is what makes an as-of fold cheap instead of a full scan."""
    day = scan["occurred_at"][:10]
    return f"{LOG_PREFIX}/occurred_date={day}/{key}.json"


def _quarantine(s3, bucket, run_id, raw, reason, received_at):
    qid = uuid.uuid4().hex[:12]
    s3.put_object(
        Bucket=bucket,
        Key=f"{QUARANTINE_PREFIX}/received_date={received_at[:10]}/{qid}.json",
        Body=json.dumps({"reason": reason, "raw": raw, "run_id": run_id,
                         "received_at": received_at}, sort_keys=True,
                        default=str).encode(),
        ContentType="application/json")


def _exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def as_sqs_event(messages):
    """Shape messages exactly as Lambda receives them from SQS, so tests and
    the demo exercise the real entry point rather than a side door."""
    return {"Records": [{"body": json.dumps(m), "messageId": uuid.uuid4().hex}
                        for m in messages]}
