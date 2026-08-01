"""Lane 2 — openFDA: where this platform is the only memory.

Neither endpoint exposes history: no as-of parameter, no vintage, no
per-record last-modified. Records mutate in place and the previous value is
gone. So: snapshot, hash each record, append an event only when the hash
changes. After a few weeks of the scheduled workflow, this repo owns a
history that does not exist anywhere else in public."""
import hashlib
import json

from .client import ResilientClient
from .normalize import normalize_record

_client = ResilientClient("openfda")

SHORTAGES_URL = "https://api.fda.gov/drug/shortages.json?limit=1000"
ENFORCEMENT_URL = "https://api.fda.gov/drug/enforcement.json?limit=1000"


def _fetch(url):
    return _client.get(url)


def _shortage_key(rec):
    basis = "|".join(str(rec.get(k, "")) for k in
                     ("generic_name", "company_name", "presentation", "package_ndc"))
    return "shortage:" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def _enforcement_key(rec):
    return "recall:" + str(rec.get("recall_number", "UNKNOWN"))


def snapshot(log, fetched_at, raw_shortages=None, raw_enforcement=None,
             prior_hashes=None):
    """Ingest one snapshot of both endpoints. raw_* bytes come either from a
    live fetch (None -> fetch here) or from a landed/fixture file. Returns the
    new {entity_id: payload_hash} map to persist for the next diff."""
    prior_hashes = prior_hashes or {}
    raw_shortages = raw_shortages or _fetch(SHORTAGES_URL)
    raw_enforcement = raw_enforcement or _fetch(ENFORCEMENT_URL)
    new_hashes = {}

    for raw, keyfn, etype, ymd, mdy, timefield in (
        (raw_shortages, _shortage_key, "fda_shortage_observed", (),
         ("initial_posting_date", "update_date", "change_date",
          "discontinued_date"), "update_date"),
        (raw_enforcement, _enforcement_key, "fda_recall_observed",
         ("report_date", "recall_initiation_date", "center_classification_date",
          "termination_date"), (), "report_date"),
    ):
        for rec in json.loads(raw).get("results", []):
            rec, _ = normalize_record(rec, date_fields_ymd=ymd, date_fields_mdy=mdy)
            entity = keyfn(rec)
            h = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()[:16]
            new_hashes[entity] = h
            if prior_hashes.get(entity) == h:
                continue                       # unchanged since last snapshot
            log.append(etype, "openfda", entity, rec,
                       event_time=rec.get(timefield) or fetched_at,
                       record_time=fetched_at)
    return new_hashes
