"""Order intake: the front door, and why a quote is a decision.

The spreadsheet tool this replaces gets the arithmetic right. What it cannot do
is answer "why was this customer quoted this number?" three weeks later, after
the exchange rate has moved. These tests are about that.
"""
from __future__ import annotations

import json

import pytest

from ingest import orders
from ingest.envelope import EventLog
from ingest.orders import OrderError, Pricing


@pytest.fixture(scope="module")
def recs():
    return orders.load_fixture()


@pytest.fixture(scope="module")
def rates():
    return orders.load_fx()


@pytest.fixture()
def pricing():
    return Pricing()


def _order(ref="X1", cad=128.0, qty=1, on="2026-09-02T09:00:00Z",
           china=None, weight=1.0, platform="微信"):
    return {"order_ref": ref, "customer": "test", "platform": platform,
            "product_name": "Thing", "quantity": qty,
            "retail_price_cad": cad, "china_price_cny": china,
            "weight_lb": weight, "ordered_at": on}


# ── effective-dated FX, the gap this lane closes ─────────────────────────────

def test_the_rate_in_force_is_read_as_of_the_order_date(rates):
    assert orders.fx_as_of(rates, "2026-08-15") == (5.12, "2026-08-01")
    assert orders.fx_as_of(rates, "2026-09-02") == (5.05, "2026-09-01")
    assert orders.fx_as_of(rates, "2026-09-12") == (4.88, "2026-09-10")


def test_the_day_a_rate_takes_effect_uses_the_new_rate(rates):
    assert orders.fx_as_of(rates, "2026-09-09")[0] == 5.05
    assert orders.fx_as_of(rates, "2026-09-10")[0] == 4.88


def test_before_the_first_rate_there_is_no_rate_not_a_default(rates):
    """The trap: defaulting to the earliest known rate would quietly price a
    July order at August's rate and nobody would ever notice."""
    assert orders.fx_as_of(rates, "2026-01-01") == (None, None)


def test_an_order_with_no_rate_in_force_is_refused(rates, pricing):
    with pytest.raises(OrderError) as e:
        orders.quote(_order(on="2026-07-15T09:00:00Z"), pricing, rates)
    assert "no exchange rate in force" in str(e.value)


# ── the quote carries what produced it ───────────────────────────────────────

def test_the_quote_records_the_rate_and_the_pricing_version(rates, pricing):
    q = orders.quote(_order(), pricing, rates)
    assert q.fx_rate == 5.05
    assert q.fx_effective_from == "2026-09-01"
    assert q.pricing_version == pricing.version


def test_the_same_order_on_two_dates_is_priced_at_two_rates(rates, pricing):
    """The reason the rate is on the event. Same product, same CAD price, two
    order dates — the arithmetic differs because the world did."""
    early = orders.quote(_order(on="2026-09-02T09:00:00Z"), pricing, rates)
    late = orders.quote(_order(on="2026-09-12T09:00:00Z"), pricing, rates)
    assert early.fx_rate != late.fx_rate
    assert early.markup_cny != late.markup_cny


def test_a_quote_stays_explainable_after_the_rate_moves(pricing):
    """A quote given on the 2nd must still read as correct at the 2nd's rate
    once the rate has moved — the same property a routing decision has with
    respect to its rule set."""
    log = EventLog()
    old_rates = [("2026-09-01", 5.05)]
    orders.replay(log, [_order("A1", on="2026-09-02T09:00:00Z")],
                  record_time="2026-09-02", pricing=pricing, rates=old_rates)
    quoted = json.loads(next(e["payload"] for e in log.events
                             if e["event_type"] == "order_quoted"))
    # the rate then moves
    new_rates = old_rates + [("2026-09-10", 4.88)]
    assert orders.fx_as_of(new_rates, "2026-09-15")[0] == 4.88
    # the stamped quote is untouched
    assert quoted["fx_rate"] == 5.05
    assert quoted["fx_effective_from"] == "2026-09-01"


# ── the arithmetic, matching the tool it replaces ────────────────────────────

def test_the_china_shelf_price_caps_the_quote(rates, pricing):
    uncapped = orders.quote(_order(china=None), pricing, rates)
    capped = orders.quote(_order(china=800), pricing, rates)
    assert capped.sell_cny < uncapped.sell_cny
    assert capped.capped_by_china_price and not uncapped.capped_by_china_price
    assert capped.sell_cny <= 800 * pricing.china_price_cap


def test_quantity_multiplies_the_total_not_the_unit_price(rates, pricing):
    one = orders.quote(_order(qty=1), pricing, rates)
    three = orders.quote(_order(qty=3), pricing, rates)
    assert three.sell_cny == one.sell_cny
    assert three.total_cny == 3 * one.sell_cny


def test_the_marketplace_commission_only_applies_on_that_platform(rates, pricing):
    plain = orders.quote(_order(platform="微信"), pricing, rates)
    market = orders.quote(_order(platform="闲鱼"), pricing, rates)
    assert market.profit_cad < plain.profit_cad


def test_freight_is_passed_through_until_the_cap_binds(rates, pricing):
    """Cost-plus while the price is free to move; once the China price pins it,
    extra weight comes straight out of the margin."""
    light = orders.quote(_order(weight=1.0), pricing, rates)
    heavy = orders.quote(_order(weight=5.0), pricing, rates)
    assert heavy.sell_cny > light.sell_cny          # passed through

    l2 = orders.quote(_order(weight=1.0, china=700), pricing, rates)
    h2 = orders.quote(_order(weight=6.0, china=700), pricing, rates)
    assert l2.sell_cny == h2.sell_cny               # pinned by the cap
    assert h2.profit_cad < l2.profit_cad
    assert h2.loses_money


# ── intake, and conservation ─────────────────────────────────────────────────

def test_orders_in_equals_quoted_plus_rejected(recs, rates, pricing):
    """The lane's conservation property. A customer waiting for a quote that
    silently vanished is the worst outcome available."""
    log = EventLog()
    c = orders.replay(log, recs, record_time="2026-09-15",
                      pricing=pricing, rates=rates)
    assert c["placed"] == len(recs) == 6
    assert c["quoted"] + c["rejected"] == len(recs)
    assert c["rejected"] == 1
    assert c["loss_making"] == 1


def test_placed_and_quoted_are_separate_events(recs, rates, pricing):
    """An order exists whether or not we could price it. Collapsing the two
    would lose every order we failed to quote, which is the set worth looking
    at."""
    log = EventLog()
    orders.replay(log, recs, record_time="2026-09-15", pricing=pricing, rates=rates)
    kinds = [e["event_type"] for e in log.events
             if e["entity_id"] == "ORDER:A1006"]
    assert "order_placed" in kinds and "order_rejected" in kinds
    assert "order_quoted" not in kinds


def test_a_rejection_carries_the_reason(recs, rates, pricing):
    log = EventLog()
    orders.replay(log, recs, record_time="2026-09-15", pricing=pricing, rates=rates)
    ev = next(e for e in log.events if e["event_type"] == "order_rejected")
    assert "no exchange rate in force" in json.loads(ev["payload"])["reason"]


def test_the_work_list_names_the_customer_still_waiting(recs, rates, pricing):
    gaps = orders.unquotable(recs, pricing, rates)
    assert [g["order_ref"] for g in gaps] == ["A1006"]
    assert gaps[0]["customer"] == "老顾客张"


def test_a_loss_making_order_is_counted_not_hidden(recs, rates, pricing):
    log = EventLog()
    c = orders.replay(log, recs, record_time="2026-09-15",
                      pricing=pricing, rates=rates)
    assert c["loss_making"] == 1
    losses = [json.loads(e["payload"]) for e in log.events
              if e["event_type"] == "order_quoted"
              and json.loads(e["payload"])["profit_cad"] <= 0]
    assert len(losses) == 1
    assert losses[0]["order_ref"] == "A1005"
    assert losses[0]["capped_by_china_price"] is True


def test_reingesting_the_same_orders_appends_nothing(recs, rates, pricing):
    log = EventLog()
    first = orders.replay(log, recs, "2026-09-15", pricing=pricing, rates=rates)
    n = len(log.events)
    second = orders.replay(log, recs, "2026-09-15", pricing=pricing, rates=rates)
    assert first["placed"] == 6 and second["placed"] == 0
    assert len(log.events) == n


def test_a_malformed_intake_line_is_refused():
    with pytest.raises(OrderError):
        orders.parse('{"order_ref": "A1"}\n')          # missing required fields
    with pytest.raises(OrderError):
        orders.parse("{not json\n")


# ── the payment link is deliberately not automatic ───────────────────────────

def test_quoting_does_not_create_a_payment_link(recs, rates, pricing):
    """A pipeline that silently creates payment objects is a pipeline that can
    charge somebody twice."""
    log = EventLog()
    orders.replay(log, recs, "2026-09-15", pricing=pricing, rates=rates)
    assert not any(e["event_type"] == "payment_link_issued" for e in log.events)


def test_a_link_is_recorded_once_it_actually_exists(recs, rates, pricing):
    log = EventLog()
    orders.replay(log, recs, "2026-09-15", pricing=pricing, rates=rates)
    orders.with_payment_link(log, "A1001", "https://buy.stripe.com/test_x",
                             "stripe", record_time="2026-09-15")
    ev = next(e for e in log.events if e["event_type"] == "payment_link_issued")
    assert json.loads(ev["payload"])["order_ref"] == "A1001"
    assert ev["entity_id"] == "ORDER:A1001"


# ── what the customer sees comes from what the ledger recorded ───────────────

def test_the_message_line_uses_the_quoted_total(recs, rates, pricing):
    o = next(r for r in recs if r["order_ref"] == "A1001")
    q = orders.quote(o, pricing, rates)
    line = orders.message_line(o, q)
    assert f"¥{q.total_cny:,.0f}" in line
    assert o["customer"] in line
    assert "款号 LW5CTVS" in line
    assert "加拿大官网价 C$128" in line
