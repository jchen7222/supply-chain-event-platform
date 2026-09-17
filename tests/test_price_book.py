"""The source price, as a dated fact.

The test that carries this file is `test_the_same_order_six_days_apart_is_
quoted_at_two_different_site_prices`. Everything else is there so that one
means something: if a price is not effective-dated, the whole platform's claim
— that any past decision can be re-derived — is false for the largest input to
every quote.
"""
import os
import tempfile

import pytest

from ingest import orders, orders_csv as oc, price_book as pb
from ingest.envelope import EventLog

RECORD_TIME = "2026-09-17"


def book():
    return pb.index(pb.load())


def order(**over):
    base = {"order_ref": "T1", "customer": "x", "product_name": "Align High-Rise Pant 25\"",
            "style_no": "LW5CTVS", "colour": "Black", "size": "8", "quantity": 1,
            "ordered_at": "2026-09-02T09:15:00Z"}
    return {**base, **over}


# ── as-of, the whole point ───────────────────────────────────────────────────

def test_a_price_is_read_as_of_the_order_date_not_today():
    b = book()
    # The site moved 128 -> 138 on 2026-09-06.
    before, _ = pb.price_as_of(b, order(), "2026-09-02")
    after, _ = pb.price_as_of(b, order(), "2026-09-08")
    assert before.retail_price_cad == 128.0
    assert before.observed_on == "2026-08-28"
    assert after.retail_price_cad == 138.0
    assert after.observed_on == "2026-09-06"


def test_the_day_the_price_changed_takes_the_new_price():
    b = book()
    on_the_day, _ = pb.price_as_of(b, order(), "2026-09-06")
    assert on_the_day.retail_price_cad == 138.0
    day_before, _ = pb.price_as_of(b, order(), "2026-09-05")
    assert day_before.retail_price_cad == 128.0


def test_an_order_before_the_first_observation_is_refused_not_back_priced():
    """A price observed on the 28th cannot have been quoted on the 1st. Filling
    it in anyway would put a number in the ledger nobody could have given."""
    b = book()
    p, why = pb.price_as_of(b, order(), "2026-08-01")
    assert p is None
    assert "no price observed on or before 2026-08-01" in why
    assert "earliest observation 2026-08-28" in why


def test_price_as_of_has_the_same_shape_as_the_other_two_lookups():
    # The shape is the argument: three kinds of reference data, one rule.
    from ingest import hts
    rate, eff = orders.fx_as_of(orders.load_fx(), "2026-09-02")
    assert rate and eff <= "2026-09-02"
    duty = hts.rate_as_of(hts.load_revisions(), "6104.63.20.11", "2026-09-02")
    assert duty is not None
    price, _ = pb.price_as_of(book(), order(), "2026-09-02")
    assert price.observed_on <= "2026-09-02"


# ── the finding ──────────────────────────────────────────────────────────────

def test_the_same_order_six_days_apart_is_quoted_at_two_different_site_prices():
    """What this lane is for.

    The same customer ordered the same trousers twice, six days apart. In
    between, the retailer put the price up C$10. Both orders are capped at the
    same Chinese shelf price, so the increase could not be passed on — and the
    margin on the second one is C$11.30 lower. That is a real fact about the
    business which is invisible without dated prices, and which a spreadsheet
    holding one price per product cannot represent at all.
    """
    recs, _, _ = oc.parse_csv(open(
        os.path.join(os.path.dirname(__file__), "..", "fixtures",
                     "orders_no_price.csv"), "rb").read())
    priced = pb.apply_to(recs, book())
    aligns = [o for o in priced if "Align High-Rise" in o["product_name"]]
    assert len(aligns) == 2

    rates = orders.load_fx()
    quotes = [orders.quote(o, orders.Pricing(), rates) for o in aligns]
    early, late = sorted(zip(aligns, quotes), key=lambda t: t[0]["ordered_at"])

    assert early[0]["retail_price_cad"] == 128.0
    assert late[0]["retail_price_cad"] == 138.0
    # capped at the same ceiling, so the customer pays the same
    assert early[1].sell_cny == late[1].sell_cny
    assert early[1].capped_by_china_price and late[1].capped_by_china_price
    # and the whole increase came out of the margin
    assert late[1].profit_cad < early[1].profit_cad
    assert round(early[1].profit_cad - late[1].profit_cad, 2) == 11.30


# ── matching, and saying how confidently ─────────────────────────────────────

def test_an_exact_match_is_recorded_as_exact():
    p, _ = pb.price_as_of(book(), order(), "2026-09-02")
    assert p.match == "style_colour_size"


def test_a_style_only_observation_matches_any_colour_and_says_so():
    # LW4BQ8S is in the book with no colour or size. An order in a colour the
    # book has never seen still prices, at a level that names the assumption.
    p, _ = pb.price_as_of(book(), order(
        style_no="LW4BQ8S", product_name="Scuba Full-Zip Hoodie",
        colour="Heathered Core Ultra Light Grey", size="S"), "2026-09-10")
    assert p.retail_price_cad == 118.0
    assert p.match == "style"


def test_an_order_with_no_style_number_matches_by_name():
    p, _ = pb.price_as_of(book(), order(
        style_no=None, product_name="Everywhere Belt Bag 1L",
        colour="Black", size="ONE SIZE"), "2026-09-03")
    assert p.retail_price_cad == 48.0
    assert p.match == "name_colour_size"


def test_matching_ignores_case_and_spacing_in_colour_and_size():
    b = book()
    for colour, size in [("true navy", "6"), ("  True Navy  ", " 6 "),
                         ("TRUE  NAVY", "6")]:
        p, why = pb.price_as_of(b, order(
            style_no="LW3HL3S", product_name="Define Jacket Luon",
            colour=colour, size=size), "2026-09-04")
        assert p is not None, why
        assert p.match == "style_colour_size", (colour, size)


def test_a_product_not_in_the_book_names_itself_in_the_reason():
    p, why = pb.price_as_of(book(), order(
        style_no="LW7BF2S", product_name="Ribbed Nulu Short"), "2026-09-12")
    assert p is None
    assert "LW7BF2S is not in the price book" in why
    assert "observe it on the site" in why


def test_out_of_stock_is_its_own_reason_separate_from_no_price():
    """Different actions: one needs a price, the other needs a different size.
    A single "cannot quote" would collapse them."""
    p, why = pb.price_as_of(book(), order(
        style_no=None, product_name="Metal Vent Tech Short Sleeve",
        colour="Graphite Grey", size="M"), "2026-09-09")
    assert p is None
    assert "out of stock" in why
    assert "2026-09-08" in why            # when it was last seen that way
    assert "check the size" in why


def test_out_of_stock_on_a_later_date_does_not_affect_an_earlier_order():
    # It was in stock when the order was placed. It going out of stock
    # afterwards is not a fact about that order.
    rows = pb.load() + [{"observed_on": "2026-09-20", "style_no": "LW5CTVS",
                         "colour": "Black", "size": "8", "in_stock": False,
                         "retail_price_cad": 138.0}]
    b = pb.index(rows)
    p, _ = pb.price_as_of(b, order(), "2026-09-02")
    assert p and p.retail_price_cad == 128.0
    gone, why = pb.price_as_of(b, order(), "2026-09-21")
    assert gone is None and "out of stock" in why


# ── filling the orders in ────────────────────────────────────────────────────

def test_apply_to_leaves_an_order_that_already_has_a_price_alone():
    # The sheet supplied one — perhaps she typed it. The book must not
    # silently overrule a human.
    o = order(retail_price_cad=999.0)
    out = pb.apply_to([o], book())
    assert out[0]["retail_price_cad"] == 999.0
    assert "price_observed_on" not in out[0]


def test_apply_to_carries_the_provenance_onto_the_order():
    out = pb.apply_to([order()], book())
    assert out[0]["retail_price_cad"] == 128.0
    assert out[0]["price_observed_on"] == "2026-08-28"
    assert out[0]["price_match"] == "style_colour_size"


def test_an_unpriceable_order_keeps_its_reason_and_is_rejected_with_it():
    o = order(style_no="NOPE", product_name="Unknown Thing")
    out = pb.apply_to([o], book())
    assert "retail_price_cad" not in out[0] or out[0].get("retail_price_cad") is None
    assert "not in the price book" in out[0]["price_gap"]

    log = EventLog()
    c = orders.replay(log, out, record_time=RECORD_TIME)
    assert c["rejected"] == 1 and c["quoted"] == 0
    ev = [e for e in log.events if e["event_type"] == "order_rejected"][0]
    assert "not in the price book" in ev["payload"]


def test_coverage_reports_what_to_go_and_look_up():
    recs, _, _ = oc.parse_csv(open(
        os.path.join(os.path.dirname(__file__), "..", "fixtures",
                     "orders_no_price.csv"), "rb").read())
    priced, gaps = pb.coverage(book(), recs)
    assert len(priced) == 10 and len(gaps) == 2
    assert {g["style_no"] or g["product_name"] for g in gaps} == {
        "LW7BF2S", "Metal Vent Tech Short Sleeve"}


def test_the_quote_records_which_observation_produced_it():
    log = EventLog()
    out = pb.apply_to([order()], book())
    orders.replay(log, out, record_time=RECORD_TIME)
    q = [e for e in log.events if e["event_type"] == "order_quoted"][0]
    import json
    p = json.loads(q["payload"])
    assert p["retail_price_cad"] == 128.0
    assert p["price_observed_on"] == "2026-08-28"
    assert p["price_match"] == "style_colour_size"
    # and the FX half of the same answer
    assert p["fx_rate"] and p["fx_effective_from"]


# ── the ledger ───────────────────────────────────────────────────────────────

def test_observations_enter_the_ledger_as_dated_events():
    log = EventLog()
    n = pb.replay(log, pb.load())
    assert n == 11
    evs = [e for e in log.events if e["event_type"] == "price_observed"]
    assert len(evs) == 11
    assert all(e["source"] == "price_book" for e in evs)
    assert all(e["entity_id"].startswith("SKU:") for e in evs)
    # event_time is the day it was observed, not the day it was loaded
    assert {e["event_time"] for e in evs} <= {
        "2026-08-28", "2026-08-30", "2026-09-01", "2026-09-06",
        "2026-09-08", "2026-09-09"}


def test_the_two_prices_for_one_sku_are_two_events_on_one_entity():
    log = EventLog()
    pb.replay(log, pb.load())
    align = [e for e in log.events if e["entity_id"] == "SKU:LW5CTVS|Black|8"]
    assert len(align) == 2
    assert [e["event_time"] for e in align] == ["2026-08-28", "2026-09-06"]


def test_reloading_the_same_book_appends_nothing_on_any_later_day():
    """The bug this caught. A SKU whose price has CHANGED has two observations,
    and the log remembers only the latest payload hash per entity — so under a
    record_time that moves with the calendar, the older observation stops
    matching and re-appends on every run. Once a day, forever, for every price
    that has ever moved.

    Stamping each observation with its own `observed_on` fixes it, which is
    what ingest/hts.py already does with effective dates."""
    log = EventLog()
    rows = pb.load()
    assert pb.replay(log, rows) == 11
    for day in (None, "2026-09-18", "2026-10-01", "2027-01-01"):
        assert pb.replay(log, rows) == 0, day
        assert len(log.events) == 11, day


def test_an_observation_is_recorded_as_known_on_the_day_it_was_observed():
    log = EventLog()
    pb.replay(log, pb.load())
    for e in log.events:
        assert e["record_time"] == e["event_time"], e["entity_id"]


def test_a_new_observation_of_a_known_sku_does_append():
    log = EventLog()
    rows = pb.load()
    pb.replay(log, rows)
    n = pb.replay(log, rows + [{
        "observed_on": "2026-09-15", "style_no": "LW5CTVS", "colour": "Black",
        "size": "8", "retail_price_cad": 142.0, "in_stock": True}])
    assert n == 1


# ── refusals on the way in ───────────────────────────────────────────────────

@pytest.mark.parametrize("line,expect", [
    ('{"style_no": "X"}', "missing"),
    ('{"observed_on": "2026-09-01"}', "missing"),
    ('{"observed_on": "2026-09-01", "retail_price_cad": 10}', "findable"),
    ('{"observed_on": "sept 1", "style_no": "X", "retail_price_cad": 10}',
     "not YYYY-MM-DD"),
    ('{"observed_on": "2026-09-01", "style_no": "X", "retail_price_cad": "ask"}',
     "not a number"),
    ('{"observed_on": "2026-09-01", "style_no": "X", "retail_price_cad": 0}',
     "must be positive"),
    ('{"observed_on": "2026-09-01", "style_no": "X", "retail_price_cad": -5}',
     "must be positive"),
    ('not json at all', "not JSON"),
])
def test_a_bad_observation_is_refused_with_its_line_number(line, expect):
    with pytest.raises(pb.PriceBookError) as e:
        pb.parse(line)
    assert expect in str(e.value)
    assert "line 1" in str(e.value)


def test_the_shipped_book_parses():
    rows = pb.load()
    assert len(rows) == 11
    assert all(r["retail_price_cad"] > 0 for r in rows)


# ── the refresh contract ─────────────────────────────────────────────────────

def test_a_pasted_table_from_the_site_becomes_dated_observations():
    pasted = ("style_no,product_name,colour,size,retail_price_cad,in_stock\n"
              "LW7BF2S,Ribbed Nulu High-Rise Short 6\",Black,4,C$78.00,yes\n"
              "LW9XX1S,Energy Bra Medium Support,White,6,68,no\n")
    rows = pb.refresh_from_csv(pasted, observed_on="2026-09-17")
    assert len(rows) == 2
    assert rows[0]["retail_price_cad"] == 78.0 and rows[0]["in_stock"] is True
    assert rows[1]["in_stock"] is False
    assert all(r["observed_on"] == "2026-09-17" for r in rows)
    assert all(r["source"] == "manual" for r in rows)


def test_the_refresh_date_comes_from_the_caller_not_the_file():
    # The one thing a paste cannot tell you is when it was taken, and a wrong
    # date here silently re-prices history.
    with pytest.raises(pb.PriceBookError):
        pb.refresh_from_csv("style_no,retail_price_cad\nX,10\n",
                            observed_on="yesterday")


@pytest.mark.parametrize("cell", ["no", "false", "0", "out", "OOS", "无货", "缺货"])
def test_out_of_stock_is_recognised_in_either_language(cell):
    rows = pb.refresh_from_csv(
        f"style_no,retail_price_cad,in_stock\nX,10,{cell}\n",
        observed_on="2026-09-17")
    assert rows[0]["in_stock"] is False


def test_appending_to_the_book_keeps_the_older_observations():
    """Append, never replace. The as-of mechanism depends entirely on the old
    rows still being there."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.jsonl")
        pb.to_jsonl([{"observed_on": "2026-09-01", "style_no": "X",
                      "retail_price_cad": 100.0}], p)
        pb.to_jsonl([{"observed_on": "2026-09-10", "style_no": "X",
                      "retail_price_cad": 110.0}], p)
        with open(p, encoding="utf-8") as f:
            rows = pb.parse(f.read())
        assert len(rows) == 2
        b = pb.index(rows)
        o = {"style_no": "X", "product_name": None, "colour": None,
             "size": None, "ordered_at": "2026-09-05"}
        early, _ = pb.price_as_of(b, o, "2026-09-05")
        late, _ = pb.price_as_of(b, o, "2026-09-11")
        assert (early.retail_price_cad, late.retail_price_cad) == (100.0, 110.0)


def test_an_empty_book_is_not_an_error_it_is_just_no_prices():
    # A first run, before anything has been observed. Every order is then a
    # work list entry, which is correct and says so.
    b = pb.index([])
    p, why = pb.price_as_of(b, order(), "2026-09-02")
    assert p is None and "not in the price book" in why


# ── the pipeline does not rewrite its own fixtures ───────────────────────────

def test_decisions_can_be_read_from_a_path_without_touching_the_fixture():
    """Regression. The orchestrator first fed the planner's output in by
    copying it over `fixtures/dispatch_decisions_sample.jsonl`. On a fresh CI
    checkout that is invisible; locally it overwrote a tracked fixture, and the
    next run read a one-line stub as the seam contract and failed somewhere
    else entirely. `--decisions` takes a path."""
    import subprocess
    import sys
    root = os.path.join(os.path.dirname(__file__), "..")
    fixture = os.path.join(root, "fixtures", "dispatch_decisions_sample.jsonl")
    before = open(fixture, "rb").read()

    with tempfile.TemporaryDirectory() as d:
        elsewhere = os.path.join(d, "fresh_decisions.jsonl")
        # Two real records, in the seam's shape, from the committed sample.
        with open(elsewhere, "wb") as f:
            f.write(b"\n".join(before.splitlines()[:2]) + b"\n")
        env = {**os.environ, "MANIFEST_DATA": d,
               "MANIFEST_DB": os.path.join(d, "m.duckdb"), "PYTHONUTF8": "1"}
        os.makedirs(os.path.join(d, "landing"), exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "ingest.run_snapshot", "--fixtures",
             "--source", "decisions", "--decisions", elsewhere],
            cwd=root, capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stderr[-1500:]
        assert "2 routing decisions ingested" in r.stdout, r.stdout

    assert open(fixture, "rb").read() == before, \
        "the committed seam fixture was modified by a run"
