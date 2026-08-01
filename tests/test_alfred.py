"""The central CI assertion: the point-in-time fold reproduces, exactly, what
the vintage archive says the series looked like on each vintage date."""
import json
import os

import pytest

from ingest import alfred
from ingest.envelope import EventLog

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "alfred_mnfctrirsa_vintages.json")


@pytest.fixture(scope="module")
def replayed():
    payload = json.load(open(FIX))
    log = EventLog()
    n, vdates = alfred.replay(log, payload)
    return payload, log, vdates


def test_every_vintage_reproduced_by_the_fold(replayed):
    payload, log, vdates = replayed
    for vd in vdates:
        expect = alfred.vintage_answer(payload, vd)
        got = {ent: json.loads(e["payload"])["value"]
               for ent, e in log.fold_current(as_of=vd).items()}
        assert got == expect, f"fold as-of {vd} must equal the vintage archive"


def test_late_added_observation_absent_before_its_vintage(replayed):
    payload, log, vdates = replayed
    early = log.fold_current(as_of=vdates[0])
    late = log.fold_current(as_of=vdates[-1])
    assert "MNFCTRIRSA:2025-12-01" not in early
    assert "MNFCTRIRSA:2025-12-01" in late


def test_revision_changes_value_between_vintages(replayed):
    payload, log, vdates = replayed
    v1 = json.loads(log.fold_current(as_of=vdates[1])["MNFCTRIRSA:2025-11-01"]["payload"])
    v2 = json.loads(log.fold_current(as_of=vdates[2])["MNFCTRIRSA:2025-11-01"]["payload"])
    assert v1["value"] != v2["value"]


@pytest.mark.skipif(not os.environ.get("FRED_API_KEY"),
                    reason="live ALFRED validation needs FRED_API_KEY")
def test_live_alfred_vintages_reproduced():
    payload = alfred.fetch_vintages_live()
    log = EventLog()
    _, vdates = alfred.replay(log, payload)
    for vd in (vdates[0], vdates[len(vdates) // 2], vdates[-1]):
        expect = alfred.vintage_answer(payload, vd)
        got = {ent: json.loads(e["payload"])["value"]
               for ent, e in log.fold_current(as_of=vd).items()}
        assert got == expect
