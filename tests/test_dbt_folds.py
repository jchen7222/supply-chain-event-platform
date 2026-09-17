"""The folds THROUGH dbt: current-state uniqueness, history chain, and the
point-in-time model reproducing an ALFRED vintage — the resume-line claim,
executed literally ('folds in dbt, validated in CI against the vintage archive')."""
import json
import os

import duckdb

from ingest import alfred, decisions, hts, orders

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "alfred_mnfctrirsa_vintages.json")


def test_current_state_one_row_per_entity(con):
    dup = con.execute("""select entity_id, count(*) c from current_state
                         group by 1 having count(*) > 1""").fetchall()
    assert dup == []


def test_history_valid_from_to_chain(con):
    bad = con.execute("""select count(*) from history
                         where valid_to is not null and valid_to < valid_from""").fetchone()[0]
    assert bad == 0


def test_registry_generated_model_covers_all_sources(con):
    gen = dict((r[0], r[1]) for r in
               con.execute("select source, events from source_event_counts").fetchall())
    raw = dict((r[0], r[1]) for r in
               con.execute("""select source, count(*) from raw.event_log
                              where not is_error_declaration group by 1""").fetchall())
    assert gen == raw


def test_point_in_time_in_dbt_matches_vintage_archive(built):
    payload = json.load(open(FIX))
    vdates, _ = alfred.vintage_columns(payload)
    vd = vdates[1]                     # middle vintage: revisions + a late add
    r = built["dbt"]("--select", "point_in_time", "--vars", f"{{as_of: '{vd}'}}")
    assert r.returncode == 0, r.stdout[-1500:]
    con = duckdb.connect(built["db"], read_only=True)
    got = dict(con.execute("""
        select entity_id, json_extract_string(payload, '$.value')
        from point_in_time where source = 'alfred'""").fetchall())
    con.close()
    assert got == alfred.vintage_answer(payload, vd)
    # restore the default build so other runs see current state
    built["dbt"]("--select", "point_in_time")


def test_duty_rate_as_of_in_dbt_matches_the_archived_revision(built):
    """The resume line, executed through dbt: the duty rate a shipment is priced
    at must equal the rate in the HTS revision in force on its ship date. Three
    dates spanning three revisions; the oracle is the archive itself, read by a
    separate code path that never touches the event log."""
    revisions = hts.load_revisions()
    for as_of in ("2024-06-01", "2025-09-01", "2026-09-01"):
        r = built["dbt"]("--select", "+duty_rate_as_of", "--vars", f"{{as_of: '{as_of}'}}")
        assert r.returncode == 0, r.stdout[-1500:]
        con = duckdb.connect(built["db"], read_only=True)
        got = dict(con.execute(
            "select hts_code, ad_valorem_rate from duty_rate_as_of").fetchall())
        con.close()
        expect = {code: hts.rate_as_of(revisions, code, as_of)
                  for code in got}
        assert got == expect, f"duty schedule as-of {as_of} must equal the archive"
    built["dbt"]("--select", "+duty_rate_as_of")      # restore the default build


def test_a_shipment_is_not_repriced_by_a_later_revision(built):
    """Same shipment, two builds. June 2024 lithium-ion owes 3.4%; by 2026 the
    rate is 7.5%. Recomputing the 2024 shipment must still produce $34."""
    def rate_on(as_of, code="8507.60.00.20"):
        assert built["dbt"]("--select", "+duty_rate_as_of",
                            "--vars", f"{{as_of: '{as_of}'}}").returncode == 0
        con = duckdb.connect(built["db"], read_only=True)
        v = con.execute("select ad_valorem_rate from duty_rate_as_of "
                        "where hts_code = ?", [code]).fetchone()[0]
        con.close()
        return v

    assert hts.landed_duty(1000.0, rate_on("2024-06-01")) == 34.0
    assert hts.landed_duty(1000.0, rate_on("2026-09-01")) == 75.0
    built["dbt"]("--select", "+duty_rate_as_of")


# ── the seam, through dbt ────────────────────────────────────────────────────

def test_landed_duty_by_order_prices_every_decision_or_says_why(con):
    """Conservation through the warehouse: one row per decision, and each row
    either carries a rate or names the reason it cannot."""
    total, priced, explained = con.execute("""
        select count(*),
               count(*) filter (where pricing_status = 'priceable'),
               count(*) filter (where pricing_status <> 'priceable')
        from landed_duty_by_order""").fetchone()
    assert total == 80
    assert priced + explained == total
    assert priced > 0 and explained == 2          # the two prohibited orders


def test_every_row_is_one_order_and_carries_the_rule_that_routed_it(con):
    dup = con.execute("""select order_id from landed_duty_by_order
                         group by 1 having count(*) > 1""").fetchall()
    assert dup == []
    nulls = con.execute("""select count(*) from landed_duty_by_order
                           where matched_rule is null
                              or rule_set_version is null""").fetchone()[0]
    assert nulls == 0


def test_a_refused_order_gets_no_heading_and_no_rate(con):
    rows = con.execute("""
        select order_id, hts_code, ad_valorem_rate, refused
        from landed_duty_by_order where pricing_status = 'no_heading'
        order by order_id""").fetchall()
    assert [r[0] for r in rows] == ["O0006", "O0023"]
    for _oid, code, rate, refused in rows:
        assert code is None and rate is None and refused is True


def test_the_model_agrees_with_the_archive_row_by_row(con, revisions=None):
    """THE ASSERTION THE SEAM EXISTS FOR, checked against an oracle.

    For every priceable order, the rate the warehouse applied must equal the
    rate the USITC archive says was in force on that order's own ship date —
    computed by hts.rate_as_of, which reads the revision fixtures directly and
    never touches the event log. Agreement is therefore evidence, not a
    restatement of the same join."""
    revs = hts.load_revisions()
    rows = con.execute("""
        select order_id, hts_code, ship_date, ad_valorem_rate, rate_effective_from
        from landed_duty_by_order
        where pricing_status = 'priceable'
        order by order_id""").fetchall()
    assert rows, "no priceable rows to check"
    for order_id, code, ship_date, rate, eff in rows:
        assert rate == hts.rate_as_of(revs, code, ship_date), \
            f"{order_id}: {code} as-of {ship_date} — warehouse {rate}"
        assert eff <= ship_date, f"{order_id}: priced at a revision from the future"


def test_headings_that_changed_on_different_dates_resolve_independently(con):
    """Why the lookup is per row: on one ship date the batch spans four
    different effective dates, because each heading's latest revision differs."""
    effs = con.execute("""
        select count(distinct rate_effective_from)
        from landed_duty_by_order where pricing_status = 'priceable'""").fetchone()[0]
    assert effs >= 3, "a single global as-of would have collapsed these"


# ── the spine: order intake -> tariff -> courier, in one row ─────────────────

def test_the_spine_has_one_row_per_order(con):
    n, dup = con.execute("""
        select count(*), count(*) - count(distinct order_ref)
        from order_to_delivery""").fetchone()
    assert n == 6 and dup == 0


def test_every_order_reaches_exactly_one_state(con):
    """Conservation, stated as coverage: no order is in no state, and the
    states partition the population."""
    rows = dict(con.execute("""select order_state, count(*)
                               from order_to_delivery group by 1""").fetchall())
    assert sum(rows.values()) == 6
    assert rows.get("rejected") == 1
    assert rows.get("delivered") == 1
    assert rows.get("needs_attention") == 1


def test_a_rejected_order_carries_its_reason_and_no_money(con):
    r = con.execute("""select rejection_reason, sell_cny, waybill
                       from order_to_delivery where order_state = 'rejected'""").fetchone()
    assert "no exchange rate in force" in r[0]
    assert r[1] is None and r[2] is None


def test_the_quote_records_the_rate_that_made_it(con):
    """Two orders for the same product at the same retail price, ten days
    apart, quoted at different rates — because the world moved."""
    rows = dict(con.execute("""
        select order_ref, fx_rate from order_to_delivery
        where product_name like 'Align%' order by order_ref""").fetchall())
    assert rows["A1001"] == 5.05
    assert rows["A1004"] == 4.88


def test_the_duty_rate_is_the_one_in_force_on_the_order_date(con):
    """Per row, not per build — the same discipline as landed_duty_by_order."""
    bad = con.execute("""select count(*) from order_to_delivery
                         where duty_effective_from is not null
                           and duty_effective_from > ordered_on""").fetchone()[0]
    assert bad == 0


def test_the_duty_rate_matches_the_archive_row_by_row(con):
    """Oracle check, same pattern as the other lanes: the archive read directly,
    never through the event log."""
    revs = hts.load_revisions()
    rows = con.execute("""select order_ref, hts_code, ordered_on, duty_rate
                          from order_to_delivery
                          where duty_rate is not null""").fetchall()
    assert rows
    for ref, code, on, rate in rows:
        assert rate == hts.rate_as_of(revs, code, on), f"{ref}: {code} as-of {on}"


def test_an_earlier_order_would_have_paid_the_pre_cut_apparel_rate():
    """The counterfactual that proves the as-of lookup is doing work. Knitted
    synthetic trousers were 28.2% until the 2026 Rev 3 cut to 16%; every order
    in the fixture is after the cut, so without this the rate change would be
    untested."""
    revs = hts.load_revisions()
    assert hts.rate_as_of(revs, "6104.63.20.11", "2026-03-19") == 0.282
    assert hts.rate_as_of(revs, "6104.63.20.11", "2026-03-20") == 0.16


def test_duty_is_the_line_the_pricing_tool_never_knew_about(con):
    """THE FINDING. The spreadsheet computes landed cost as retail + tax +
    freight + handling. Apparel duty is 16-32%, so it is usually the largest
    single line — and for at least one order it is the difference between a
    profit and a loss."""
    rows = con.execute("""
        select order_ref, quoted_profit_cad, duty_cad, true_profit_cad
        from order_to_delivery where duty_cad is not null
        order by order_ref""").fetchall()
    assert rows
    for ref, quoted, duty, true in rows:
        assert duty > 0
        assert abs(true - (quoted - duty)) < 0.01, ref
        assert true < quoted, f"{ref}: duty must reduce the margin"

    flipped = [r for r in rows if r[1] > 0 and r[3] < 0]
    assert flipped, "expected at least one order that duty turns into a loss"
    assert flipped[0][0] == "A1002"


def test_the_last_scan_is_the_latest_by_occurrence(con):
    """A1003's delivery webhook arrived before its customs-clearance scan.
    Ordering by arrival would leave it reading as still in customs."""
    r = con.execute("""select last_status, scan_count from order_to_delivery
                       where order_ref = 'A1003'""").fetchone()
    assert r == ("delivered", 3)


def test_a_customs_hold_surfaces_as_needs_attention(con):
    r = con.execute("""select order_ref, last_status from order_to_delivery
                       where order_state = 'needs_attention'""").fetchone()
    assert r == ("A1002", "customs_held")


def test_a_parcel_with_no_order_is_reported(con):
    """Reconciliation in the uncomfortable direction: is anything coming out
    that we never took in? One waybill is being scanned that no order accounts
    for — a work list, not a build failure."""
    rows = con.execute("""select waybill, scan_count, last_status
                          from parcels_without_orders""").fetchall()
    assert rows == [("YT8806", 2, "in_transit")]


def test_no_order_appears_as_both_shipped_and_unshipped(con):
    bad = con.execute("""select count(*) from order_to_delivery
                         where waybill is null
                           and order_state not in ('rejected','quoted_not_shipped')"""
                      ).fetchone()[0]
    assert bad == 0
