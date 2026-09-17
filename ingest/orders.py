"""Lane 7 — order intake: the front door of the business.

WHAT THIS REPLACES. A spreadsheet tool (`add_order.py`) that took an order from
WeChat or 闲鱼, looked up the product page, worked out a landed cost, applied a
markup, capped the price against the Chinese shelf price, wrote a row, and
printed the line to send back to the customer. That tool is correct and it is
what the business actually runs on. What it cannot do is answer a question
three weeks later: **why was this customer quoted this number?**

Because a quote is not an observation of the world — it is a decision, like a
routing decision, and it is only defensible if you can reconstruct the inputs
that produced it. Three things move underneath a quote:

    the exchange rate          moves daily
    the pricing settings       markup, freight per lb, the China-price cap
    the site price             the retailer changes it whenever it likes

So the quote event carries `fx_rate`, `fx_effective_from` and
`pricing_version`, for exactly the reason a routing decision carries
`rule_set_version`: a quote given on Tuesday must still read as correct at
Tuesday's rate after the rate has moved. "You quoted me ¥915" is then a query,
not an argument.

THE GAP THIS CLOSES. Until now nothing in this platform knew about currency.
Duty rates were effective-dated; the exchange rate was not modelled at all.
`fixtures/fx_rates.json` fixes that — FX as effective-dated reference data,
read as-of the quote date, by the same mechanism as the tariff schedule. It is
the third piece of reference data in the system and it follows the same rule:
a rate is a fact with a date, never a number in a config file.

WHAT THIS DELIBERATELY DOES NOT DO. It does not create the payment link.
`add_order.py` calls Stripe, which is correct there — it is a tool a human runs
and watches. A pipeline that silently creates payment objects is a pipeline
that can charge somebody twice. So the ledger records the quote and the
*intent*; the link id is appended later, once something actually created one.
Recording a link that was never created would put a lie in an append-only log,
where it is permanent.

CONSERVATION. `orders in the file = quoted + rejected`. An order that cannot be
priced is rejected with a reason and stays visible, because a customer waiting
for a quote that silently vanished is the worst outcome available.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace

FIX_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

REQUIRED = ("order_ref", "customer", "product_name", "quantity",
            "retail_price_cad", "ordered_at")

# The pricing block, versioned. A change to any of these is a new version, and
# every quote records which version produced it — the same discipline the
# routing rules use, for the same reason.
PRICING_VERSION = "p1"


class OrderError(ValueError):
    """The intake file is not the agreed shape. Raised, not skipped."""


@dataclass(frozen=True)
class Pricing:
    """Settings as of `version`. Kept immutable so a quote cannot be recomputed
    later under different numbers by accident."""
    version: str = PRICING_VERSION
    sales_tax: float = 0.13        # tax paid at the source retailer
    freight_per_lb_cad: float = 6.50
    handling_cad: float = 2.00
    default_weight_lb: float = 1.0
    markup: float = 0.22
    rounding_step_cny: float = 5.0
    platform_fee: float = 0.01     # marketplace commission
    platform_fee_cap_cny: float = None
    china_price_cap: float = 0.85  # never quote above this share of the local price


@dataclass(frozen=True)
class Quote:
    landed_cad: float
    markup_cny: float
    sell_cny: float
    total_cny: float
    profit_cad: float
    capped_by_china_price: bool
    fx_rate: float
    fx_effective_from: str
    pricing_version: str

    @property
    def loses_money(self):
        return self.profit_cad <= 0


# ── effective-dated FX, the third piece of reference data ────────────────────

def load_fx(name="fx_rates.json"):
    """[(effective_from, rate)] ascending — CNY per CAD."""
    with open(os.path.join(FIX_DIR, name), encoding="utf-8") as f:
        doc = json.load(f)
    return sorted(((r["effective_from"], float(r["cny_per_cad"]))
                   for r in doc["rates"]), key=lambda r: r[0])


def fx_as_of(rates, on_date):
    """The rate in force on a date — the latest one effective on or before it.
    Returns (rate, effective_from), or (None, None) before the first rate.

    Same shape as hts.rate_as_of, deliberately: a quote recomputed for a past
    date must use that date's rate, not today's."""
    in_force = [r for r in rates if r[0] <= on_date]
    if not in_force:
        return None, None
    eff, rate = in_force[-1]
    return rate, eff


# ── the quote ────────────────────────────────────────────────────────────────

def quote(order, pricing, rates):
    """Price one order. Raises OrderError when it cannot be priced — never
    returns a plausible guess."""
    on = order["ordered_at"][:10]
    rate, eff = fx_as_of(rates, on)
    if rate is None:
        raise OrderError(f"no exchange rate in force on {on}")

    # Said plainly, because this is the common case for a spreadsheet drop: the
    # order form records what the customer wants, not what the retailer charges
    # for it. float(None) would say "must be real number, not NoneType", which
    # reads like a bug in the pipeline rather than a missing column in the file.
    if order.get("retail_price_cad") in (None, ""):
        raise OrderError("no source price supplied — awaiting product lookup")

    cad = float(order["retail_price_cad"])
    qty = int(order["quantity"])
    weight = float(order.get("weight_lb") or pricing.default_weight_lb)
    china = order.get("china_price_cny")

    landed = cad * (1 + pricing.sales_tax) + weight * pricing.freight_per_lb_cad \
        + pricing.handling_cad
    step = pricing.rounding_step_cny
    markup_cny = math.ceil(landed * (1 + pricing.markup) * rate / step) * step

    sell, capped = markup_cny, False
    if china:
        ceiling = math.floor(float(china) * pricing.china_price_cap / step) * step
        if ceiling < sell:
            sell, capped = ceiling, True

    total = qty * sell
    fee = 0.0
    if order.get("platform") in ("闲鱼", "xianyu"):
        fee = total * pricing.platform_fee
        if pricing.platform_fee_cap_cny is not None:
            fee = min(fee, pricing.platform_fee_cap_cny)
    profit = (total - fee) / rate - qty * landed

    return Quote(round(landed, 2), round(markup_cny, 2), round(sell, 2),
                 round(total, 2), round(profit, 2), capped,
                 rate, eff, pricing.version)


# ── intake ───────────────────────────────────────────────────────────────────

def parse(blob):
    """JSON lines of orders -> [record]. A malformed line is refused."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    out = []
    for i, line in enumerate(blob.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise OrderError(f"line {i}: not JSON: {e}") from e
        missing = [k for k in REQUIRED if rec.get(k) in (None, "")]
        if missing:
            raise OrderError(f"line {i}: missing {missing}")
        out.append(rec)
    return out


def load_fixture(name="orders_sample.jsonl"):
    with open(os.path.join(FIX_DIR, name), encoding="utf-8") as f:
        return parse(f.read())


def replay(log, orders, record_time, pricing=None, rates=None):
    """Two events per order that prices, one per order that does not.

    `order_placed` is the observation: the customer asked for this.
    `order_quoted` is the decision: we offered this number, at this rate, under
    this pricing version.
    `order_rejected` when it cannot be priced, carrying the reason.

    Separating placed from quoted matters — an order exists whether or not we
    could price it, and collapsing them would lose every order we failed to
    quote, which is exactly the set worth looking at.
    """
    pricing = pricing or Pricing()
    rates = rates if rates is not None else load_fx()
    counts = {"placed": 0, "quoted": 0, "rejected": 0, "loss_making": 0}

    for o in orders:
        entity = f"ORDER:{o['order_ref']}"
        if log.append("order_placed", "order_intake", entity,
                      {k: o.get(k) for k in
                       ("order_ref", "customer", "platform", "product_name",
                        "style_no", "colour", "size", "quantity",
                        "retail_price_cad", "china_price_cny", "weight_lb",
                        "ordered_at", "product_url", "hts_code", "waybill")},
                      event_time=o["ordered_at"][:10], record_time=record_time):
            counts["placed"] += 1

        try:
            q = quote(o, pricing, rates)
        except (OrderError, TypeError, ValueError) as e:
            if log.append("order_rejected", "order_intake", entity,
                          {"order_ref": o["order_ref"], "reason": str(e),
                           "pricing_version": pricing.version},
                          event_time=o["ordered_at"][:10],
                          record_time=record_time):
                counts["rejected"] += 1
            continue

        # Counted only when something was actually appended. The counts this
        # returns are printed as the receipt for an upload, so a re-drop of an
        # unchanged file must report zero — "7 quoted" for work that was
        # recognised as a replay and skipped is a lie told to the one person
        # who is relying on it.
        if log.append("order_quoted", "order_intake", entity,
                      {"order_ref": o["order_ref"],
                       "landed_cad": q.landed_cad,
                       "sell_cny": q.sell_cny,
                       "total_cny": q.total_cny,
                       "profit_cad": q.profit_cad,
                       "capped_by_china_price": q.capped_by_china_price,
                       "fx_rate": q.fx_rate,
                       "fx_effective_from": q.fx_effective_from,
                       "pricing_version": q.pricing_version},
                      event_time=o["ordered_at"][:10], record_time=record_time):
            counts["quoted"] += 1
            if q.loses_money:
                counts["loss_making"] += 1

    return counts


def unquotable(orders, pricing=None, rates=None):
    """Orders that cannot be priced, with the reason — the work list."""
    pricing = pricing or Pricing()
    rates = rates if rates is not None else load_fx()
    out = []
    for o in orders:
        try:
            quote(o, pricing, rates)
        except (OrderError, TypeError, ValueError) as e:
            out.append({"order_ref": o["order_ref"], "customer": o.get("customer"),
                        "reason": str(e)})
    return out


def message_line(order, q):
    """The line sent back to the customer, unchanged from the tool it replaces.
    Kept here because it is the only part of the quote the customer ever sees,
    and it should come from the same numbers the ledger recorded."""
    bits = [f"{order['customer']}：{order['product_name']}"]
    if order.get("style_no"):
        bits.append(f"款号 {order['style_no']}")
    if order.get("colour"):
        bits.append(f"颜色 {order['colour']}")
    if order.get("size"):
        bits.append(f"尺码 {order['size']}")
    head = " | ".join(bits)
    return (f"{head} × {order['quantity']} | ¥{q.total_cny:,.0f} | "
            f"加拿大官网价 C${float(order['retail_price_cad']):.0f}，官网直邮，约3–4周")


def with_payment_link(log, order_ref, link_url, provider, record_time):
    """Append the payment link ONCE IT EXISTS. Separate from the quote on
    purpose — see the module docstring on why a pipeline does not create
    payment objects."""
    return log.append("payment_link_issued", "order_intake",
                      f"ORDER:{order_ref}",
                      {"order_ref": order_ref, "provider": provider,
                       "link_url": link_url},
                      event_time=record_time, record_time=record_time)
