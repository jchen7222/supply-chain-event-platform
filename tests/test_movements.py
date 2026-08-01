"""Recall drill: real recall + real NDCs, synthetic movements, bitemporal gap."""
import os

from ingest import movements
from ingest.envelope import EventLog

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
ENF = os.path.join(FIX, "openfda_enforcement.json")


def _setup():
    recall = movements.pick_recall(ENF)
    log = EventLog()
    n = movements.generate(log, recall["ndcs"], seed=7)
    return recall, log, n


def test_recall_is_real_and_ndc_linked():
    recall, _, _ = _setup()
    assert recall["recall_number"] and recall["ndcs"]


def test_drill_locates_every_lot():
    recall, log, _ = _setup()
    rows = movements.drill(log, recall)
    assert len(rows) == 3 * len(recall["ndcs"])
    assert all(r["holder"].startswith("PH-") for r in rows), \
        "current state: every lot delivered to a pharmacy"


def test_as_of_shows_what_we_believed_not_what_happened():
    recall, log, _ = _setup()
    morning = movements.drill(log, recall, as_of="2026-07-30T12:00")
    now = movements.drill(log, recall)
    m = {r["lot"]: r["holder"] for r in morning}
    c = {r["lot"]: r["holder"] for r in now}
    moved_late = [l for l in c if m.get(l) != c[l]]
    assert moved_late, "the late-recorded receive must separate belief from fact"
    for l in moved_late:
        assert m[l].startswith("DEPOT"), "at noon the system still believed depot"


def test_replay_is_a_no_op():
    recall, log, n1 = _setup()
    n2 = movements.generate(log, recall["ndcs"], seed=7)
    assert n1 > 0 and n2 == 0, "same fixtures, second pass, zero new events"
