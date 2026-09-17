"""Lane 8 — the source price, as effective-dated reference data.

THE CORRECTION THIS EXISTS FOR. The intake parser was built expecting a price
column, and that was wrong about how the business works. A customer fills in
what they want: a product, a colour, a size, a quantity. **The price is an
output.** It is looked up on the retailer's site, marked up, capped against the
Chinese shelf price and sent back. Asking the customer for it would be asking
them to do the one part of the job they are paying for.

So the order sheet has no price in it, and `order_placed` legitimately arrives
without one. This module is where the number comes from.

WHY A BOOK AND NOT A LOOKUP. The obvious design is: read the order, fetch the
page, use today's price. That is wrong for the same reason using today's duty
rate is wrong, and this repository already refuses to do it twice:

    duty rate    effective-dated   ingest/hts.py       rate_as_of()
    FX rate      effective-dated   ingest/orders.py    fx_as_of()
    site price   effective-dated   HERE                price_as_of()

A retailer changes its prices whenever it likes. A quote given on Tuesday must
still read as correct at Tuesday's price on Friday, after the price has moved —
otherwise "why did you quote me ¥915?" has no answer, and a margin that looks
wrong in hindsight cannot be told apart from one that was wrong at the time.

So an observed price is a **fact with a date**: `price_observed` on entity
`SKU:<key>`, and `price_as_of(book, key, on_date)` returns the price that was
in force on the order's own date. Three pieces of reference data, one rule, one
shape. That is the point of the platform.

WHAT IT REFUSES.

  * **An order dated before the first observation.** No price is in force, so
    there is no quote. Back-pricing an order at a price observed afterwards
    would put a number in the ledger that nobody could have quoted, which is
    worse than an empty cell.
  * **A guess dressed as a match.** Every lookup records HOW it matched —
    exact, or by style ignoring colour, or by name — because "priced off the
    style number, ignoring the colour" is a different confidence from an exact
    hit, and the difference belongs in the quote, not in somebody's memory.
  * **Stock.** Out of stock at the last observation is reported as its own
    reason, separate from "no price", because the two need different actions:
    one needs a price, the other needs a different size.

HOW THE BOOK GETS FILLED. Deliberately not this module's business — see
`refresh_from_csv` at the bottom for the contract. Whether the observations
come from a scraping provider, a browser session or a paste from the site, they
arrive as dated rows and enter the ledger the same way. The pipeline does not
care, and it never invents one.
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass

FIX_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

REQUIRED = ("observed_on", "retail_price_cad")

# How a lookup matched, best first. Recorded on every quote.
MATCH_LEVELS = ("style_colour_size", "style_colour", "style",
                "name_colour_size", "name_colour", "name")


class PriceBookError(ValueError):
    """The book is not the agreed shape. Raised, not skipped."""


@dataclass(frozen=True)
class Priced:
    retail_price_cad: float
    observed_on: str
    match: str
    in_stock: bool
    product_url: str = None
    style_no: str = None

    @property
    def stale_days(self):
        return None      # filled by price_as_of, which knows the order date


def _norm(s):
    """Fold a key component. 'True Navy ' and 'true navy' are the same colour;
    'ONE SIZE', 'One Size' and 'onesize' are the same size."""
    if s is None:
        return ""
    return re.sub(r"[\s\-_/]+", "", str(s).strip().lower())


def keys_for(order):
    """The lookup keys to try, most specific first, paired with the match level
    each one would record.

    The order is the judgement. Style plus colour plus size is exact. Falling
    back to style alone says "this retailer prices a style the same in every
    colour", which is usually true and is still an assumption — so it is
    recorded rather than hidden. Name-based keys come last because two
    different products can share a name across seasons."""
    style, colour, size = (_norm(order.get("style_no")),
                           _norm(order.get("colour")), _norm(order.get("size")))
    name = _norm(order.get("product_name"))
    out = []
    if style:
        out += [((style, colour, size), "style_colour_size"),
                ((style, colour, ""), "style_colour"),
                ((style, "", ""), "style")]
    if name:
        out += [(("n:" + name, colour, size), "name_colour_size"),
                (("n:" + name, colour, ""), "name_colour"),
                (("n:" + name, "", ""), "name")]
    # De-duplicate while keeping order: an order with no colour produces the
    # same key twice, and trying it twice would not be wrong, only confusing.
    seen, uniq = set(), []
    for k, level in out:
        if k not in seen:
            seen.add(k)
            uniq.append((k, level))
    return uniq


def index(rows):
    """[observation] -> {key: [(observed_on, observation)] ascending}.

    Every observation is filed under every key it could answer, so one row with
    a style number, a colour and a size serves an exact lookup and both of the
    broader ones. That is why the same row can be found by a `style` lookup
    when no exact match exists."""
    book = {}
    for r in rows:
        style, colour, size = (_norm(r.get("style_no")), _norm(r.get("colour")),
                               _norm(r.get("size")))
        name = _norm(r.get("product_name"))
        keys = []
        if style:
            keys += [(style, colour, size), (style, colour, ""), (style, "", "")]
        if name:
            keys += [("n:" + name, colour, size), ("n:" + name, colour, ""),
                     ("n:" + name, "", "")]
        for k in set(keys):
            book.setdefault(k, []).append((r["observed_on"], r))
    for k in book:
        book[k].sort(key=lambda t: t[0])
    return book


def price_as_of(book, order, on_date):
    """The price in force on a date, or None.

    Same shape as `hts.rate_as_of` and `orders.fx_as_of`, deliberately: the
    latest observation effective on or before the date, never a later one.

    Returns (Priced, None) or (None, reason). A reason is a sentence somebody
    can act on, because it ends up in the order's rejection event and then on
    her screen."""
    tried = []
    for key, level in keys_for(order):
        entries = book.get(key)
        if not entries:
            continue
        tried.append(level)
        in_force = [(d, r) for d, r in entries if d <= on_date]
        if not in_force:
            first = entries[0][0]
            return None, (f"no price observed on or before {on_date} "
                          f"(earliest observation {first}) — add the price that "
                          f"was on the site that day, or re-date the order")
        observed_on, row = in_force[-1]
        if row.get("in_stock") is False:
            return None, (f"last seen out of stock on {observed_on} "
                          f"(matched by {level}) — check the size, or re-observe")
        return Priced(
            retail_price_cad=float(row["retail_price_cad"]),
            observed_on=observed_on,
            match=level,
            in_stock=bool(row.get("in_stock", True)),
            product_url=row.get("product_url"),
            style_no=row.get("style_no"),
        ), None

    what = (order.get("style_no") or order.get("product_name") or "?")
    return None, (f"{what} is not in the price book — observe it on the site "
                  f"and add a row" + (f" (tried {', '.join(tried)})" if tried else ""))


# ── loading ──────────────────────────────────────────────────────────────────

def parse(blob):
    """JSON lines of observations -> [record]. A malformed line is refused."""
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
            raise PriceBookError(f"line {i}: not JSON: {e}") from e
        missing = [k for k in REQUIRED if rec.get(k) in (None, "")]
        if missing:
            raise PriceBookError(f"line {i}: missing {missing}")
        if not (rec.get("style_no") or rec.get("product_name")):
            raise PriceBookError(
                f"line {i}: needs a style_no or a product_name to be findable")
        try:
            rec["retail_price_cad"] = float(rec["retail_price_cad"])
        except (TypeError, ValueError) as e:
            raise PriceBookError(
                f"line {i}: retail_price_cad {rec['retail_price_cad']!r} "
                f"is not a number") from e
        if rec["retail_price_cad"] <= 0:
            raise PriceBookError(f"line {i}: price must be positive")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(rec["observed_on"])):
            raise PriceBookError(
                f"line {i}: observed_on {rec['observed_on']!r} is not YYYY-MM-DD")
        out.append(rec)
    return out


def load(name="price_book.jsonl"):
    path = os.path.join(FIX_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def replay(log, rows, record_time=None):
    """One `price_observed` event per observation, on entity `SKU:<style|name>`.

    The price history goes in the ledger for the same reason the duty schedule
    does: so a quote can be re-derived from events alone, with no file on
    anybody's laptop involved.

    `record_time` DEFAULTS TO EACH ROW'S `observed_on`, and that is not a
    detail. Stamping these with "today" instead breaks replay idempotence, and
    I had it wrong first: a SKU with two observations — 128 on the 28th, 138 on
    the 6th — has only the LATEST payload hash remembered per entity, so on a
    re-run under a new record_time the older 128 no longer matches and appends
    a second time. The log would grow a spurious event for every price that has
    ever changed, every single day. `ingest/hts.py` already solved this by
    stamping each duty rate with its own effective date, and the same reasoning
    applies here with less strain: an observation IS the act of learning, so
    the day somebody looked at the site is the day the repository knew. The
    delay between looking and loading the file is file transport, not
    knowledge.

    The parameter is kept for the genuine late-arrival case — an observation
    written down on paper and entered weeks later — where the two dates really
    do differ. Passing today's date for a routine load is the mistake above."""
    n = 0
    for r in rows:
        ident = r.get("style_no") or ("n:" + str(r.get("product_name")))
        bits = [ident, r.get("colour") or "", r.get("size") or ""]
        entity = "SKU:" + "|".join(str(b) for b in bits).rstrip("|")
        if log.append("price_observed", "price_book", entity,
                      {k: r.get(k) for k in
                       ("style_no", "product_name", "colour", "size",
                        "retail_price_cad", "in_stock", "product_url",
                        "observed_on", "source")},
                      event_time=r["observed_on"],
                      record_time=record_time or r["observed_on"]):
            n += 1
    return n


def coverage(book, orders):
    """Which orders the book can price, and why the rest cannot.

    Returns (priced, gaps). This is what gets printed after a spreadsheet drop:
    a work list of exactly what to go and look up, rather than a count."""
    priced, gaps = [], []
    for o in orders:
        if o.get("retail_price_cad") not in (None, ""):
            continue                     # the sheet supplied one; nothing to do
        p, why = price_as_of(book, o, o["ordered_at"][:10])
        if p:
            priced.append((o, p))
        else:
            gaps.append({"order_ref": o.get("order_ref"),
                         "product_name": o.get("product_name"),
                         "style_no": o.get("style_no"),
                         "colour": o.get("colour"), "size": o.get("size"),
                         "reason": why})
    return priced, gaps


def apply_to(orders, book):
    """Fill in `retail_price_cad` from the book, in place of nothing.

    Every filled order also carries `price_observed_on` and `price_match`, so
    the quote event records not just the number but which observation produced
    it and how confidently it matched. An order the book cannot price is left
    exactly as it was — `orders.replay` then rejects it with the book's own
    reason, and it shows up as a work list instead of a silent gap."""
    out = []
    for o in orders:
        if o.get("retail_price_cad") not in (None, ""):
            out.append(o)
            continue
        p, why = price_as_of(book, o, o["ordered_at"][:10])
        if not p:
            out.append({**o, "price_gap": why})
            continue
        out.append({**o,
                    "retail_price_cad": p.retail_price_cad,
                    "price_observed_on": p.observed_on,
                    "price_match": p.match,
                    "product_url": o.get("product_url") or p.product_url,
                    "style_no": o.get("style_no") or p.style_no})
    return out


# ── how observations get in ───────────────────────────────────────────────────
#
# THE CONTRACT, and nothing more. Where the numbers come from is a separate
# job with its own failure modes — a scraping provider's response shape, a
# session that expired, a page that changed. None of that belongs in the
# pricing path, and none of it is implemented here, because an untested
# adapter that looks finished is worse than an obvious gap.
#
# What every source must produce is the same four things: what was observed,
# on what date, at what price, and whether it was in stock. Anything that can
# produce those rows can fill the book — a provider's API, a browser session,
# or a person reading the site and pasting a column into a spreadsheet. The
# last one works today, which is the point.

REFRESH_COLUMNS = ("style_no", "product_name", "colour", "size",
                   "retail_price_cad", "in_stock", "product_url")


def refresh_from_csv(blob, observed_on, source="manual"):
    """A pasted table of what the site says today -> [observation].

    The date is supplied by the caller rather than read from the file, because
    the one thing a paste cannot tell you is when it was taken, and a wrong
    date here silently re-prices history."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8-sig")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(observed_on)):
        raise PriceBookError(f"observed_on {observed_on!r} is not YYYY-MM-DD")
    rows = []
    reader = csv.DictReader(blob.splitlines())
    for i, raw in enumerate(reader, start=2):
        rec = {k: (raw.get(k) or "").strip() or None for k in REFRESH_COLUMNS}
        if not (rec["style_no"] or rec["product_name"]):
            continue                     # a blank line from a paste
        price = re.sub(r"[^\d.]", "", str(rec["retail_price_cad"] or ""))
        if not price:
            raise PriceBookError(f"line {i}: no price")
        rec["retail_price_cad"] = float(price)
        rec["in_stock"] = str(raw.get("in_stock", "")).strip().lower() not in (
            "no", "false", "0", "out", "oos", "无货", "缺货")
        rec["observed_on"] = observed_on
        rec["source"] = source
        rows.append(rec)
    return rows


def to_jsonl(rows, path, append=True):
    """Append observations to the book. Append, because an observation is a
    fact about a day and a new one never replaces an old one — the whole
    as-of mechanism depends on the old rows still being there."""
    mode = "a" if append and os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)
