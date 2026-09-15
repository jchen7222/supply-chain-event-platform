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
