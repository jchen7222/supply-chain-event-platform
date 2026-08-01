"""Lane 2 properties: normalization traps (all confirmed live), hash-diff memory."""
import json
import os

from ingest import openfda
from ingest.envelope import EventLog
from ingest.normalize import normalize_record

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def _raws():
    return (open(os.path.join(FIX, "openfda_shortages.json"), "rb").read(),
            open(os.path.join(FIX, "openfda_enforcement.json"), "rb").read())


def test_dates_normalized_to_iso_and_logged():
    raw_s, raw_e = _raws()
    log = EventLog()
    openfda.snapshot(log, "2026-08-01", raw_s, raw_e)
    shortages = [json.loads(e["payload"]) for e in log.events
                 if e["event_type"] == "fda_shortage_observed"]
    recalls = [json.loads(e["payload"]) for e in log.events
               if e["event_type"] == "fda_recall_observed"]
    assert shortages and recalls
    for r in shortages:
        for f in ("initial_posting_date", "update_date"):
            if f in r:
                assert "-" in r[f] and "/" not in r[f], f"{f} must be ISO"
    for r in recalls:
        if "report_date" in r:
            assert "-" in r["report_date"], "YYYYMMDD must become ISO"
    assert any("_normalizations" in r for r in shortages + recalls)


def test_status_is_ongoing_not_on_going():
    _, raw_e = _raws()
    statuses = {r.get("status") for r in json.loads(raw_e)["results"]}
    assert "On-Going" not in statuses
    assert statuses <= {"Ongoing", "Completed", "Terminated"}


def test_misspelled_enum_fixed_and_logged():
    rec, notes = normalize_record({"availability": "Avaliable"})
    assert rec["availability"] == "Available"
    assert notes == ["enum:availability:Avaliable->Available"]


def test_hash_diff_second_snapshot_is_silent():
    raw_s, raw_e = _raws()
    log = EventLog()
    h1 = openfda.snapshot(log, "2026-08-01", raw_s, raw_e)
    n_first = len(log.events)
    h2 = openfda.snapshot(log, "2026-08-02", raw_s, raw_e, prior_hashes=h1)
    assert len(log.events) == n_first, "unchanged records must emit nothing"
    assert h1 == h2


def test_hash_diff_catches_a_mutation():
    raw_s, raw_e = _raws()
    log = EventLog()
    h1 = openfda.snapshot(log, "2026-08-01", raw_s, raw_e)
    d = json.loads(raw_e)
    d["results"][0]["termination_date"] = "20260724"     # the Z-0522-2022 pattern
    mutated = json.dumps(d).encode()
    n_before = len(log.events)
    openfda.snapshot(log, "2026-08-02", raw_s, mutated, prior_hashes=h1)
    new = [e for e in log.events[n_before:]]
    assert len(new) == 1 and new[0]["record_time"] == "2026-08-02"
    assert json.loads(new[0]["payload"])["termination_date"] == "2026-07-24"
