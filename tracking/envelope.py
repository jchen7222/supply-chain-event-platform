"""Courier scan envelope: two carrier formats, one canonical shape.

WHY THIS LOOKS LIKE INDUSTRIAL TELEMETRY, AND WHY THAT IS NOT A COINCIDENCE.
A courier tracking feed has the same awkward properties as a field gateway:

  * scans arrive LATE — a depot scan from Tuesday reaches the webhook Friday
  * scans arrive OUT OF ORDER — "delivered" can land before "customs cleared"
  * scans arrive TWICE — webhooks are retried, at-least-once by design
  * scans get REVISED — a status miscoded at the depot is corrected later
  * every carrier has its own schema, and none of them will change it for you

So the machinery is the same: an append-only log, an event time separate from a
receipt time, and a fold that answers as-of a date. What differs is the domain,
and one design choice that follows from it — see tracking/handler.py on why
this lane buffers on SQS rather than Kinesis.

THE QUESTION THE BITEMPORAL SPLIT ANSWERS HERE. "Where was this parcel on
Wednesday?" and "where did we believe it was on Wednesday?" are different
questions whenever a scan arrives late, and a customer service conversation
needs the second one: what did the tracking page say when the customer looked
at it. `occurred_at` is when the scan happened at the depot; `received_at` is
when the webhook reached us. Both are on every event.

TWO CARRIER FORMATS, BOTH LIVE. Carriers do not migrate on your schedule, so
the platform upgrades at the edge and rejects what it does not know — into
quarantine with a reason, never by dropping the batch:

  v1 (legacy):  {schema: 1, scan_id, waybill, status_text, scanned_at, site}
  v2 (current): {schema: 2, scan_id, waybill, status_code, occurred_at,
                 location: {site, country}, carrier}
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

KNOWN_SCHEMAS = {1, 2}
V1_REQUIRED = {"scan_id", "waybill", "status_text", "scanned_at"}
V2_REQUIRED = {"scan_id", "waybill", "status_code", "occurred_at"}

# The canonical status vocabulary. Deliberately small: every carrier word maps
# onto one of these, and a word we do not recognise is quarantined rather than
# guessed — a mis-mapped customs status is the difference between "arriving
# Tuesday" and "we need another document from you".
STATUSES = ("accepted", "in_transit", "export_customs", "import_customs",
            "customs_cleared", "customs_held", "out_for_delivery",
            "delivered", "exception", "returned")

TERMINAL = ("delivered", "returned")

# Carrier vocabulary -> canonical. The Chinese phases are what a China-outbound
# consolidator actually receives; the numeric codes are the DHL-style feed.
STATUS_MAP = {
    # SF / YunExpress style, Chinese phase names
    "已收件": "accepted", "已揽收": "accepted",
    "运输中": "in_transit", "已发出": "in_transit", "干线运输": "in_transit",
    "出口报关": "export_customs", "已交海关": "export_customs",
    "进口清关中": "import_customs", "清关中": "import_customs",
    "清关完成": "customs_cleared", "海关放行": "customs_cleared",
    "海关查验": "customs_held", "需补充文件": "customs_held",
    "派送中": "out_for_delivery", "已签收": "delivered",
    "异常": "exception", "退回": "returned",
    # English text feeds
    "picked up": "accepted", "in transit": "in_transit",
    "export clearance": "export_customs", "import clearance": "import_customs",
    "customs cleared": "customs_cleared", "held by customs": "customs_held",
    "with courier": "out_for_delivery", "delivered": "delivered",
    "exception": "exception", "returned to sender": "returned",
    # numeric codes
    "10": "accepted", "20": "in_transit", "30": "export_customs",
    "40": "import_customs", "45": "customs_cleared", "47": "customs_held",
    "50": "out_for_delivery", "60": "delivered", "80": "exception",
    "90": "returned",
}

WAYBILL = re.compile(r"^[A-Z0-9][A-Z0-9\-]{5,31}$")


class ScanError(ValueError):
    """Raised with a human-readable reason; the reason becomes the quarantine
    record, so it is written for whoever reads the quarantine report."""


def _ts(value, field):
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ScanError(f"{field} is not an ISO timestamp: {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def canonical_status(raw):
    """Carrier word -> canonical status, or raise. Never guesses: an unknown
    customs phrase quarantined is a question for a human; an unknown customs
    phrase mapped to 'in_transit' is a customer told the wrong thing."""
    key = str(raw).strip().lower()
    if key in STATUS_MAP:
        return STATUS_MAP[key]
    if str(raw).strip() in STATUS_MAP:            # Chinese keys are case-exact
        return STATUS_MAP[str(raw).strip()]
    if key in STATUSES:
        return key
    raise ScanError(f"unmapped carrier status: {raw!r}")


def upgrade_v1(scan):
    """Legacy flat scan -> canonical v2 shape. The carrier that sends v1 has no
    country on the scan, so `country` is absent rather than assumed."""
    return {
        "schema": 2,
        "scan_id": scan["scan_id"],
        "waybill": scan["waybill"],
        "status_code": scan["status_text"],
        "occurred_at": scan["scanned_at"],
        "location": {"site": scan.get("site")},
        "carrier": scan.get("carrier") or "legacy",
        "upgraded_from": 1,
    }


def validate(scan):
    """Canonical scan, or ScanError with the reason. Called at the edge so a
    bad scan never reaches the log."""
    if not isinstance(scan, dict):
        raise ScanError("payload is not an object")
    schema = scan.get("schema")
    if schema not in KNOWN_SCHEMAS:
        raise ScanError(f"unknown schema: {schema!r}")

    required = V1_REQUIRED if schema == 1 else V2_REQUIRED
    missing = sorted(f for f in required if scan.get(f) in (None, ""))
    if missing:
        raise ScanError(f"missing required fields: {', '.join(missing)}")

    if schema == 1:
        scan = upgrade_v1(scan)

    waybill = str(scan["waybill"]).strip().upper()
    if not WAYBILL.match(waybill):
        raise ScanError(f"implausible waybill: {scan['waybill']!r}")

    out = {
        "schema": 2,
        "scan_id": str(scan["scan_id"]).strip(),
        "waybill": waybill,
        "status": canonical_status(scan["status_code"]),
        "status_raw": str(scan["status_code"]),
        "occurred_at": _ts(scan["occurred_at"], "occurred_at"),
        "site": (scan.get("location") or {}).get("site"),
        "country": (scan.get("location") or {}).get("country"),
        "carrier": scan.get("carrier") or "unknown",
    }
    if scan.get("upgraded_from"):
        out["upgraded_from"] = scan["upgraded_from"]
    return out


def content_key(scan):
    """Identity for deduplication.

    NOT the carrier's scan_id alone. Carriers reuse ids across waybills and
    occasionally resend the same id with a corrected status — so the key is the
    waybill, the scan id AND a hash of what the scan says. A retried webhook
    collapses; a genuine correction under the same id lands as a new event and
    the fold picks it up by occurred_at.
    """
    body = json.dumps({k: scan[k] for k in
                       ("waybill", "scan_id", "status", "occurred_at", "site")},
                      sort_keys=True)
    return f"{scan['waybill']}:{scan['scan_id']}:" \
           f"{hashlib.sha256(body.encode()).hexdigest()[:12]}"


def is_terminal(status):
    return status in TERMINAL
