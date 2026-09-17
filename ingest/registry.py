"""The source registry. Adding a source = one row here (mirrored in the dbt
seed) plus its small mapping module. The GSCPI row is the proof — see the
pinned commit in the README."""

REGISTRY = [
    {"source": "alfred",   "lane": "vintage_replay",
     "event_type": "series_value_observed",
     "natural_key": "series:observation_date", "cadence": "on_release",
     "has_native_history": True},
    {"source": "openfda",  "lane": "snapshot_hash_diff",
     "event_type": "fda_shortage_observed|fda_recall_observed",
     "natural_key": "shortage_fingerprint|recall_number", "cadence": "daily",
     "has_native_history": False},
    {"source": "imf_imts", "lane": "mirror_reconciliation",
     "event_type": "trade_mirror_observed",
     "natural_key": "pair:year", "cadence": "monthly",
     "has_native_history": False},
    {"source": "pharma_synthetic", "lane": "epcis_movement_stream",
     "event_type": "epcis_commission|epcis_aggregate|epcis_ship|epcis_receive",
     "natural_key": "lot:ndc:lot_no", "cadence": "on_demand",
     "has_native_history": True},
    {"source": "usitc_hts", "lane": "effective_dated_reference",
     "event_type": "duty_rate_observed",
     "natural_key": "hts_code", "cadence": "on_revision",
     "has_native_history": True},
    # The front door: a customer order, and the quote we gave for it. A quote
    # is a decision, so it carries the FX rate and pricing version that made
    # it — see ingest/orders.py.
    {"source": "order_intake", "lane": "decision_with_reference_data",
     "event_type": "order_placed|order_quoted|order_rejected|payment_link_issued",
     "natural_key": "order_ref", "cadence": "continuous",
     "has_native_history": False},
    # The third piece of effective-dated reference data, alongside the duty
    # schedule and the FX rate. An observed retail price is a fact with a date,
    # because a retailer moves its prices whenever it likes and a quote given
    # on Tuesday must still read as correct at Tuesday's price. Same shape as
    # usitc_hts: record_time is the day it was observed, which is what makes a
    # full replay a no-op. See ingest/price_book.py.
    {"source": "price_book", "lane": "effective_dated_reference",
     "event_type": "price_observed",
     "natural_key": "style_no|product_name:colour:size", "cadence": "on_observation",
     "has_native_history": True},
    # Courier webhooks through API Gateway -> SQS -> Lambda -> S3, folded here.
    # Scans arrive late, out of order and duplicated; see tracking/.
    {"source": "courier_tracking", "lane": "webhook_event_log",
     "event_type": "shipment_scanned",
     "natural_key": "waybill:scan_id", "cadence": "continuous",
     "has_native_history": True},
    # The loop-closing lane: not an observation of the world, but a decision
    # made by another system, ingested so it is subject to the same fold and
    # the same audit as everything else. Contract: ingest/decisions.py.
    {"source": "dispatch_planner", "lane": "decision_ingest",
     "event_type": "routing_decided",
     "natural_key": "order_id", "cadence": "per_plan",
     "has_native_history": False},
    {"source": "gscpi",    "lane": "snapshot_hash_diff",
     "event_type": "index_value_observed",
     "natural_key": "series:observation_date", "cadence": "monthly",
     "has_native_history": False},
]


def write_seed(path):
    cols = list(REGISTRY[0].keys())
    with open(path, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in REGISTRY:
            f.write(",".join(str(r[c]) for c in cols) + "\n")
