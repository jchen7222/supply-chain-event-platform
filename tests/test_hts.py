"""The duty-rate dimension, and the assertion the whole lane exists for:
the rate applied to a shipment must equal the rate in the revision in force
on its ship date — not the rate in force today.

ALFRED proves the bitemporal fold on a macro series. This proves it on the
reference data the platform actually reasons about, against an archive nobody
here can alter.
"""
import json

import pytest

from ingest import hts
from ingest.envelope import EventLog

LITHIUM = "8507.60.00.20"
COSMETIC = "3304.99.50.00"
HANDBAG = "4202.21.60.00"
MILK = "0402.10.50.00"        # specific duty — cents/kg
DRESS = "6104.43.20.10"       # compound — % + cents/kg


@pytest.fixture(scope="module")
def revisions():
    return hts.load_revisions()


@pytest.fixture(scope="module")
def replayed(revisions):
    log = EventLog()
    n = hts.replay(log, revisions)
    return log, n


def _folded_rate(log, code, as_of):
    ev = log.fold_current(as_of=as_of).get(f"HTS:{code}")
    return None if ev is None else json.loads(ev["payload"])["ad_valorem_rate"]


# ── the parse refuses rather than guesses ────────────────────────────────────

def test_free_is_zero_not_missing():
    assert hts.parse_rate("Free") == (0.0, "free")


def test_ad_valorem_becomes_a_fraction():
    assert hts.parse_rate("3.4%") == (0.034, "ad_valorem")


def test_a_specific_duty_is_flagged_never_coerced_to_a_number():
    rate, kind = hts.parse_rate("3.3 cents/kg")
    assert rate is None and kind == "specific", \
        "cents/kg cannot be multiplied by a declared value"


def test_a_compound_duty_is_flagged():
    rate, kind = hts.parse_rate("16% + 2.5 cents/kg")
    assert rate is None and kind == "compound"


def test_landed_duty_refuses_a_non_ad_valorem_rate():
    assert hts.landed_duty(1000.0, 0.075) == 75.0
    with pytest.raises(ValueError):
        hts.landed_duty(1000.0, None)


def test_header_lines_carry_no_rate_and_are_skipped(revisions):
    codes = {c for c, _, _ in hts.rows(revisions[0][2])}
    assert "3304" not in codes, "a heading is structure, not a tariff line"
    assert COSMETIC in codes


# ── the log holds changes, and the fold reconstructs the schedule ────────────

def test_only_changed_rates_emit_a_second_event(replayed):
    log, _ = replayed
    # lithium changed once (rev5 -> rev12), so two events; cosmetics changed
    # once at rev3; the dress rate never changed, so exactly one event
    assert len(log.fold_history(f"HTS:{LITHIUM}")) == 2
    assert len(log.fold_history(f"HTS:{COSMETIC}")) == 2
    assert len(log.fold_history(f"HTS:{DRESS}")) == 1, \
        "an unchanged rate must not re-emit on every revision"


def test_a_rate_change_is_a_new_row_the_old_row_still_readable(replayed):
    log, _ = replayed
    assert _folded_rate(log, LITHIUM, "2024-06-01") == 0.034
    assert _folded_rate(log, LITHIUM, "2025-09-01") == 0.075


def test_a_rate_is_absent_before_its_first_revision(replayed):
    log, _ = replayed
    assert _folded_rate(log, LITHIUM, "2020-01-01") is None


# ── THE ASSERTION ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("on_date", [
    "2024-05-15", "2024-12-31", "2025-06-30",      # 2024 Rev 5 in force
    "2025-07-01", "2025-11-11", "2026-03-19",      # 2025 Rev 12 in force
    "2026-03-20", "2026-09-01",                    # 2026 Rev 3 in force
])
@pytest.mark.parametrize("code", [LITHIUM, COSMETIC, HANDBAG])
def test_fold_equals_the_revision_in_force_on_that_date(replayed, revisions, code, on_date):
    log, _ = replayed
    assert _folded_rate(log, code, on_date) == hts.rate_as_of(revisions, code, on_date), \
        f"{code} as-of {on_date} must equal the archived revision in force"


def test_a_past_shipment_does_not_reprice_under_todays_rate(replayed, revisions):
    """The failure this lane exists to prevent. A lithium-battery shipment that
    left in June 2024 owes 3.4%. Two revisions later the rate is 7.5%. Recompute
    the 2024 shipment and it must still owe 3.4% — $34, not $75."""
    log, _ = replayed
    shipped_at, declared = "2024-06-01", 1000.0

    at_ship_date = hts.landed_duty(declared, _folded_rate(log, LITHIUM, shipped_at))
    at_today = hts.landed_duty(declared, _folded_rate(log, LITHIUM, "2026-09-01"))

    assert at_ship_date == 34.0
    assert at_today == 75.0
    assert at_ship_date != at_today, "the whole point: rules move, history does not"


def test_every_code_in_every_revision_reconciles(replayed, revisions):
    """Exhaustive: every ad valorem code, at every revision boundary."""
    log, _ = replayed
    for effective_from, _label, payload in revisions:
        for code, _desc, general in hts.rows(payload):
            if hts.parse_rate(general)[1] != "ad_valorem":
                continue
            assert _folded_rate(log, code, effective_from) == \
                hts.rate_as_of(revisions, code, effective_from), \
                f"{code} at {effective_from}"


def test_replaying_the_same_revisions_twice_adds_nothing(revisions):
    """Idempotency, the first promise — a re-ingest is a no-op."""
    log = EventLog()
    first = hts.replay(log, revisions)
    second = hts.replay(log, revisions)
    assert first > 0 and second == 0
