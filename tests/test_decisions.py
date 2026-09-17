"""The seam: routing decisions from the dispatch planner.

The fixture here was produced by the other repository, not written by hand:

    python -m dispatch.run --seed 1 --emit-decisions \\
        fixtures/dispatch_decisions_sample.jsonl --ship-date 2026-09-01

So these tests pin the contract from the receiving side. `dispatch-planner`'s
tests/test_export.py pins the same shape from the sending side; if the two ever
disagree, someone changed the contract without saying so.
"""
import json

import pytest

from ingest import decisions, hts
from ingest.decisions import ContractError
from ingest.envelope import EventLog

LITHIUM = "8507.60.00.20"
COSMETIC = "3304.99.50.00"
PERFUME = "3303.00.30.00"


@pytest.fixture(scope="module")
def records():
    return decisions.load_fixture()


@pytest.fixture(scope="module")
def revisions():
    return hts.load_revisions()


def _rec(order_id, cls, hts_code, ship_date="2026-09-01", services=("Standard",),
         rule="R6", weight=10.0):
    return {"order_id": order_id, "commodity_class": cls, "hts_code": hts_code,
            "weight_kg": weight, "eligible_services": list(services),
            "rule_set_version": "v1", "matched_rule": rule, "reason": "",
            "ship_date": ship_date, "decision_seq": 0}


# ── the contract, read strictly ──────────────────────────────────────────────

def test_the_fixture_is_the_shape_the_planner_writes(records):
    assert len(records) == 80
    for r in records:
        assert set(decisions.REQUIRED) <= set(r)
    assert {r["ship_date"] for r in records} == {"2026-09-01"}


def test_blank_lines_are_tolerated_malformed_ones_are_not():
    good = json.dumps(_rec("O1", "general", "4202.21.60.00"))
    assert len(decisions.parse(good + "\n\n" + good.replace("O1", "O2") + "\n")) == 2
    with pytest.raises(ContractError):
        decisions.parse(good + "\n{not json\n")


def test_a_missing_field_is_refused_not_defaulted():
    """A decision we cannot read is not a decision we may drop."""
    bad = _rec("O1", "general", "4202.21.60.00")
    del bad["matched_rule"]
    with pytest.raises(ContractError) as e:
        decisions.parse(json.dumps(bad))
    assert "matched_rule" in str(e.value)


def test_a_json_array_is_not_json_lines():
    with pytest.raises(ContractError):
        decisions.parse('[1, 2, 3]')


# ── landing them as events ───────────────────────────────────────────────────

def test_every_record_becomes_exactly_one_event(records):
    """Conservation across the seam, from this side."""
    log = EventLog()
    n = decisions.replay(log, records, record_time="2026-09-15")
    assert n == len(records) == 80
    assert len(log.events) == 80


def test_the_entity_follows_the_house_convention(records):
    log = EventLog()
    decisions.replay(log, records, record_time="2026-09-15")
    ids = {e["entity_id"] for e in log.events}
    assert all(i.startswith("ORDER:") for i in ids)
    assert "ORDER:O0006" in ids          # one of the refused orders


def test_the_bitemporal_split_is_ship_date_vs_ingest_date(records):
    """event_time is when the shipment moves; record_time is when we learned."""
    log = EventLog()
    decisions.replay(log, records, record_time="2026-09-15")
    ev = next(e for e in log.events if e["entity_id"] == "ORDER:O0000")
    assert ev["event_time"] == "2026-09-01"
    assert ev["record_time"] == "2026-09-15"


def test_reingesting_the_same_file_appends_nothing(records):
    log = EventLog()
    first = decisions.replay(log, records, record_time="2026-09-15")
    second = decisions.replay(log, records, record_time="2026-09-15")
    assert first == 80 and second == 0


def test_replanning_onto_a_different_sailing_is_a_new_fact():
    """The divergence from hts.py, asserted. There the revision label was left
    out of the payload because it was 1:1 with the effective date across a
    quarter-million rows. Here the ship date IS part of what was decided, so it
    stays in the payload and a re-plan appends instead of being swallowed."""
    log = EventLog()
    r1 = _rec("O1", "lithium_battery", LITHIUM, ship_date="2026-09-01")
    r2 = dict(r1, ship_date="2026-10-01")
    assert decisions.replay(log, [r1], record_time="2026-09-15") == 1
    assert decisions.replay(log, [r2], record_time="2026-09-20") == 1
    assert len(log.fold_history("ORDER:O1")) == 2


def test_an_unchanged_decision_re_exported_later_is_a_no_op():
    """The other half of that: same order, same decision, same sailing, ingested
    on two different days is still one fact."""
    log = EventLog()
    r = _rec("O1", "general", "4202.21.60.00")
    assert decisions.replay(log, [r], record_time="2026-09-15") == 1
    assert decisions.replay(log, [r], record_time="2026-09-16") == 0


# ── the gaps are reported, not hidden ────────────────────────────────────────

def test_a_refused_order_is_reported_as_refused_not_unclassified(records):
    gaps = {g["order_id"]: g for g in decisions.unpriceable(records)}
    assert set(gaps) == {"O0006", "O0023"}
    for g in gaps.values():
        assert g["why"] == "refused"
        assert g["commodity_class"] == "prohibited"
        assert g["matched_rule"] == "R1"


def test_a_shippable_order_with_no_heading_is_a_classification_gap():
    """Distinct from a refusal, and it must be visible: a missing heading on
    something that was going to ship means the classification config has a hole,
    and silently pricing it at zero would hide that."""
    recs = [_rec("O9", "novelty", None, services=("Standard",), rule="R6")]
    g = decisions.unpriceable(recs)[0]
    assert g["why"] == "unclassified"


def test_every_priced_order_has_a_heading(records):
    priced = [r for r in records if r["hts_code"]]
    gaps = decisions.unpriceable(records)
    assert len(priced) + len(gaps) == len(records)


# ── the answer neither repository has alone ──────────────────────────────────

@pytest.mark.parametrize("ship_date,expected", [
    ("2024-06-01", 0.034),      # 2024 Rev 5 in force
    ("2025-06-30", 0.034),      # still Rev 5 the day before the change
    ("2025-07-01", 0.075),      # 2025 Rev 12 takes effect
    ("2026-09-01", 0.075),      # unchanged since
])
def test_a_decision_is_priced_at_the_rate_in_force_on_its_own_ship_date(
        revisions, ship_date, expected):
    """The whole point of the seam, in Python before it is asserted in dbt.
    The same routing decision, on four different sailings, owes four rates —
    read from the archive by a code path that never touches the event log."""
    log = EventLog()
    decisions.replay(log, [_rec("O1", "lithium_battery", LITHIUM,
                                ship_date=ship_date, rule="R2")],
                     record_time="2026-09-15")
    ev = log.fold_current().get("ORDER:O1")
    payload = json.loads(ev["payload"])
    assert payload["ship_date"] == ship_date
    assert hts.rate_as_of(revisions, payload["hts_code"], ship_date) == expected


def test_the_cosmetics_heading_moves_later_than_the_battery_one(revisions):
    """Two headings, two different change dates — which is why the lookup has
    to be per row. A single as-of for the whole batch would be wrong for one of
    them on any date between the two revisions."""
    assert hts.rate_as_of(revisions, LITHIUM, "2026-01-01") == 0.075
    assert hts.rate_as_of(revisions, COSMETIC, "2026-01-01") == 0.0
    assert hts.rate_as_of(revisions, COSMETIC, "2026-09-01") == 0.049


def test_a_free_heading_is_zero_not_missing(revisions):
    assert hts.rate_as_of(revisions, PERFUME, "2026-09-01") == 0.0
