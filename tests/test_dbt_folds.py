"""The folds THROUGH dbt: current-state uniqueness, history chain, and the
point-in-time model reproducing an ALFRED vintage — the resume-line claim,
executed literally ('folds in dbt, validated in CI against the vintage archive')."""
import json
import os

import duckdb

from ingest import alfred, decisions, hts

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
