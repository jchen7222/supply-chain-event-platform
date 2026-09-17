"""Lane 6 — routing decisions from the dispatch planner.

WHAT MAKES THIS LANE DIFFERENT. Every other source here observes the outside
world: a tariff revision, a drug shortage, a trade statistic. This one ingests
a *decision made by another system* — and a decision is a fact about us, not
about the world. It is the lane that closes the loop: the planner decides,
the decision lands here as an event, and from then on it is subject to the same
fold, the same as-of reconstruction, and the same audit as any observation.

That is the point of the seam. Two questions become answerable that neither
repository can answer alone:

    which parcels did rule R3 route in September?                 (the planner knows)
    what duty rate was in force on each one's ship date?          (this repo knows)
    -> which parcels did R3 route, and what did each one owe?     (only together)

THE CONTRACT. `dispatch-planner` writes one JSON object per decision:

    {"order_id": "O0042", "commodity_class": "flammable_liquid",
     "hts_code": "3303.00.30.00", "weight_kg": 2.0,
     "eligible_services": ["DGExpress"], "rule_set_version": "v1",
     "matched_rule": "R3", "reason": "", "ship_date": "2026-09-01",
     "decision_seq": 12}

produced by:

    python -m dispatch.run --seed 42 --emit-decisions decisions.jsonl \\
                           --ship-date 2026-09-01

Neither repository imports the other and neither has to be running. The
contract is that shape, and both sides pin it against a sample — theirs in
`tests/test_export.py`, ours in `fixtures/dispatch_decisions_sample.jsonl`.

THE BITEMPORAL SPLIT, APPLIED HONESTLY. `event_time` is the ship date: when
the shipment moves in the world. `record_time` is the day we ingested the file:
when this repository learned of it. So a decision exported today for a shipment
that left last month lands with event_time in the past and record_time now —
and a fold as-of last month correctly does not see it, because we did not know
it then.

WHY ship_date IS IN THE PAYLOAD, unlike the revision label in hts.py. There,
the label was 1:1 with the effective date across 249 revisions x ~19,000 codes,
so including it would have turned every code into a "change" at every revision.
Here the entity is one order, the volume is one row per order, and the ship date
is part of what was decided: re-planning the same order onto a different sailing
is a new fact about it, and the payload hash has to move so the append is not
swallowed as a no-op. Same principle, opposite conclusion, because the shape of
the data is different.
"""
import json
import os

FIX_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

REQUIRED = ("order_id", "commodity_class", "eligible_services",
            "rule_set_version", "matched_rule", "ship_date")

PAYLOAD_FIELDS = ("commodity_class", "hts_code", "weight_kg",
                  "eligible_services", "rule_set_version", "matched_rule",
                  "reason", "ship_date")


class ContractError(ValueError):
    """The upstream file is not the shape we agreed. Raised rather than
    skipped: a decision we cannot read is not a decision we may drop."""


def parse(blob):
    """Bytes or str of JSON lines -> [record]. Blank lines are allowed (a file
    written by a shell loop often ends with one); a malformed line is not."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    out = []
    for i, line in enumerate(blob.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ContractError(f"line {i}: not JSON: {e}") from e
        if not isinstance(rec, dict):
            raise ContractError(f"line {i}: expected an object, got {type(rec).__name__}")
        missing = [k for k in REQUIRED if k not in rec]
        if missing:
            raise ContractError(f"line {i}: missing {missing}")
        out.append(rec)
    return out


def load_fixture(name="dispatch_decisions_sample.jsonl"):
    with open(os.path.join(FIX_DIR, name), encoding="utf-8") as f:
        return parse(f.read())


def replay(log, records, record_time):
    """Append one `routing_decided` event per record. Returns the number
    appended — re-ingesting the same file appends nothing."""
    n = 0
    for rec in records:
        payload = {k: rec.get(k) for k in PAYLOAD_FIELDS}
        if log.append("routing_decided", "dispatch_planner",
                      f"ORDER:{rec['order_id']}", payload,
                      event_time=rec["ship_date"], record_time=record_time):
            n += 1
    return n


def unpriceable(records):
    """Records that cannot be assigned a duty rate, and why.

    Two distinct cases, kept distinct on purpose:
      * refused    — the planner would not ship it, so it has no heading
      * unclassified — it was shippable but no tariff heading was mapped

    The first is correct behaviour; the second is a gap in the classification
    config and should be visible rather than silently priced at zero.
    """
    out = []
    for r in records:
        if r.get("hts_code"):
            continue
        refused = not r.get("eligible_services")
        out.append({"order_id": r["order_id"],
                    "commodity_class": r.get("commodity_class"),
                    "matched_rule": r.get("matched_rule"),
                    "why": "refused" if refused else "unclassified"})
    return out
