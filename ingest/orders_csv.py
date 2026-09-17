"""CSV intake — the front door's front door.

WHY THIS EXISTS. `orders.py` reads JSON lines, which is a shape a program
produces. The business produces a spreadsheet: a WeChat order form exported
from Excel, with Chinese column headers, a currency symbol in the price
column, a trailing blank row, and — if it was saved on a Chinese Windows
machine — GB18030 encoding rather than UTF-8. Refusing that file because it
is not JSONL would mean the pipeline only accepts input the pipeline already
produced, which is the failure mode of every data platform nobody uses.

So this module's whole job is the boundary: take the file that actually
exists, turn it into the records `orders.replay` already knows how to price,
and account for every row it could not.

THREE THINGS IT REFUSES TO GUESS.

1. `retail_price_cad`. If the export has no price column, this does NOT invent
   one. Every row still enters the ledger as `order_placed` — the customer did
   ask for it — and is then `order_rejected` with "no source price supplied".
   That is the point of splitting placed from quoted: an order exists whether
   or not we could price it, and the set we failed to price is the work list.
   Filling it in is the product-lookup half that is not built yet.

2. Ambiguous dates. `9/2/2026` is 2 September or 9 February depending on the
   locale of the machine that saved it, and the difference changes which FX
   rate the quote is computed at. The convention here is month-first
   (`DAY_FIRST = False`), matching a North-American Excel; `--day-first`
   overrides it. Either way the row's original date string is preserved in the
   payload as `ordered_at_source`, so a wrong convention is recoverable from
   the ledger rather than baked into it.

3. `order_ref`. When the export has no order number, one is derived from a
   hash of (customer, product, date) and flagged `order_ref_derived`. That
   matters because of how this file gets uploaded: a human drops a CSV, and
   humans drop the same CSV twice. A stable ref means the second drop hits
   `EventLog.append`'s idempotence and appends nothing. A random ref would
   double every order. The flag exists so a derived ref is never mistaken for
   one of hers.

CONSERVATION. `rows in the file = accepted + refused`. A refusal carries the
line number and the reason, and the run continues — one bad row in a hundred
should not cost the other ninety-nine their quotes. A file whose *header* is
unusable is a different thing and raises, because 100 identical row errors is
not a work list, it is a misconfigured column mapping.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import re

from .orders import OrderError

# Month-first, per a North-American Excel export. See docstring point 2.
DAY_FIRST = False

# Encodings in the order worth trying. utf-8-sig first because "CSV UTF-8" in
# any modern Excel writes a BOM, and a BOM read as UTF-8 turns the first header
# into "﻿订单号", which matches nothing. gb18030 second because it is what
# plain "CSV (Comma delimited)" produces on a Chinese-locale Windows, and it is
# a superset of GBK/GB2312 so it covers all three.
ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")

# Header aliases. Left: the field orders.quote expects. Right: spellings seen
# in real exports. Compared after _norm(), so case, spaces, underscores and
# full-width punctuation are already gone.
ALIASES = {
    "order_ref":       ("orderref", "orderid", "orderno", "ordernumber", "order",
                        "订单号", "订单编号", "单号", "编号", "序号"),
    "customer":        ("customer", "customername", "name", "buyer", "client",
                        "客户", "客户名", "客户姓名", "姓名", "买家", "买家昵称",
                        "微信名", "微信昵称", "收件人"),
    "platform":        ("platform", "channel", "source", "渠道", "平台", "来源"),
    "product_name":    ("productname", "product", "item", "itemname", "description",
                        "商品", "商品名称", "产品", "产品名称", "品名", "货品"),
    "style_no":        ("styleno", "stylenumber", "sku", "style", "modelno",
                        "款号", "货号", "型号", "款式编号"),
    "colour":          ("colour", "color", "颜色", "色号", "配色"),
    "size":            ("size", "尺码", "尺寸", "规格", "码数"),
    "quantity":        ("quantity", "qty", "count", "units", "数量", "件数", "个数"),
    "retail_price_cad": ("retailpricecad", "retailprice", "pricecad", "cadprice",
                         "canadaprice", "sourceprice", "cost", "price",
                         "加拿大价", "加拿大官网价", "官网价", "原价", "成本价",
                         "加币价", "加元价"),
    "china_price_cny": ("chinapricecny", "chinaprice", "cnyprice", "localprice",
                        "国内价", "国内售价", "中国价", "天猫价", "参考价", "人民币价"),
    "weight_lb":       ("weightlb", "weight", "lbs", "重量", "磅重", "重量磅"),
    "ordered_at":      ("orderedat", "orderdate", "date", "datetime", "placedat",
                        "createdat", "下单时间", "下单日期", "订单日期", "日期", "时间"),
    "product_url":     ("producturl", "url", "link", "productlink",
                        "链接", "商品链接", "产品链接", "网址"),
    "hts_code":        ("htscode", "hts", "tariffcode", "hscode",
                        "税号", "海关编码", "商品编码"),
    "waybill":         ("waybill", "tracking", "trackingno", "trackingnumber", "awb",
                        "运单号", "快递单号", "物流单号", "单号追踪"),
}

# Required to make a record at all. `retail_price_cad` is deliberately NOT here
# — see docstring point 1.
CSV_REQUIRED = ("customer", "product_name", "quantity", "ordered_at")

NUMERIC = ("retail_price_cad", "china_price_cny", "weight_lb")

_STRIP_MONEY = re.compile(r"[^\d.\-]")
_STRIP_QTY = re.compile(r"[^\d.\-]")


class CsvHeaderError(OrderError):
    """The header row cannot be mapped. A file-level fault, not a row fault."""


def _norm(h):
    """Fold a header to a comparable token: no case, no spaces, no separators,
    no BOM, no full-width brackets. '  Retail Price (CAD) ' -> 'retailpricecad'."""
    h = (h or "").replace("﻿", "").strip().lower()
    h = h.replace("（", "(").replace("）", ")").replace("　", "")
    return re.sub(r"[\s_\-/\\.()\[\]:：、,，*#]", "", h)


def decode(blob):
    """Bytes -> text, trying the encodings a spreadsheet actually gets saved in.
    Returns (text, encoding_used) so the run can say which one it was; a file
    that only decodes as latin-1 is usually a mis-saved GB18030 file and the
    label is the clue."""
    if isinstance(blob, str):
        return blob, "str"
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16"), "utf-16"
    for enc in ENCODINGS:
        try:
            return blob.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise CsvHeaderError("file is not text in any of " + ", ".join(ENCODINGS))


def _dialect(sample):
    """Comma, semicolon or tab. Excel on a European or Chinese locale writes
    semicolons; 'Unicode Text' writes tabs."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def map_header(header):
    """[raw column] -> {field: index}. Raises when nothing required is present.

    First match wins, so a file with both 'price' and 'retail_price_cad' takes
    the explicit one only if it appears first — which is why the alias lists put
    the specific spellings ahead of the generic ones."""
    found, unmapped = {}, []
    for i, raw in enumerate(header):
        tok = _norm(raw)
        if not tok:
            continue
        for field, names in ALIASES.items():
            if field in found:
                continue
            if tok in names:
                found[field] = i
                break
        else:
            unmapped.append(raw.strip())
    missing = [f for f in CSV_REQUIRED if f not in found]
    if missing:
        raise CsvHeaderError(
            f"header is missing {missing}; columns seen: {unmapped or header}. "
            f"Add the column, or rename it to one of: "
            + "; ".join(f"{m}={'/'.join(_suggest(m))}" for m in missing))
    return found, unmapped


def _suggest(field, n=3):
    """A few accepted spellings, in both scripts. Taking the first n off the
    alias list gives all-English suggestions for every field, which is useless
    advice to hand somebody whose file has Chinese headers — the only case
    where this message ever appears."""
    names = ALIASES[field]
    ascii_ = [x for x in names if x.isascii()][:n]
    cjk = [x for x in names if not x.isascii()][:n]
    return ascii_ + cjk


def money(v):
    """'C$128.00' / '¥1,080' / '128' / '' -> float or None. Refuses a value that
    has digits but will not parse, rather than reading it as zero: a price
    silently becoming 0.00 produces a confident quote at a loss."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "—", "n/a", "N/A", "NA", "#N/A"):
        return None
    neg = s.startswith("(") and s.endswith(")")    # accounting negative
    cleaned = _STRIP_MONEY.sub("", s)
    if not cleaned or cleaned in (".", "-"):
        raise ValueError(f"not a number: {s!r}")
    try:
        f = float(cleaned)
    except ValueError as e:
        raise ValueError(f"not a number: {s!r}") from e
    return -f if neg else f


def quantity(v):
    """'2' / '2件' / '2.0' / '两件' -> int. Fractional quantities are refused;
    half a pair of leggings is a typo, not an order."""
    s = str(v or "").strip()
    cleaned = _STRIP_QTY.sub("", s)
    if not cleaned:
        raise ValueError(f"no quantity in {s!r}")
    f = float(cleaned)
    if f != int(f):
        raise ValueError(f"fractional quantity {s!r}")
    if int(f) <= 0:
        raise ValueError(f"quantity must be positive, got {s!r}")
    return int(f)


_CN_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")


def when(v, day_first=DAY_FIRST):
    """A date cell -> 'YYYY-MM-DDTHH:MM:SSZ'. Raises on anything it cannot read.

    Only the quote's *date* matters downstream (fx_as_of and rate_as_of both
    slice at [:10]), so a missing time becomes midnight rather than a refusal.
    An unreadable date IS a refusal, because the wrong date means the wrong
    exchange rate and a quote nobody can defend."""
    s = str(v or "").strip()
    if not s:
        raise ValueError("no order date")

    m = _CN_DATE.search(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}T00:00:00Z"

    # Excel serial: days since 1899-12-30 (Excel's leap-year bug included).
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        n = float(s)
        if 20000 <= n <= 80000:
            base = dt.datetime(1899, 12, 30, tzinfo=dt.timezone.utc)
            t = base + dt.timedelta(days=n)
            return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    iso = s.replace("/", "-").replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(iso)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    order = ("%d-%m-%Y", "%m-%d-%Y") if day_first else ("%m-%d-%Y", "%d-%m-%Y")
    cand = s.replace("/", "-").split()
    datepart = cand[0]
    timepart = cand[1] if len(cand) > 1 else "00:00"
    # A first component above 12 can only be a day, whatever the convention.
    head = datepart.split("-")[0]
    if head.isdigit() and int(head) > 12:
        order = ("%d-%m-%Y",)
    for fmt in order:
        for tf in ("%H:%M:%S", "%H:%M", ""):
            try:
                t = dt.datetime.strptime(
                    f"{datepart} {timepart}".strip() if tf else datepart,
                    f"{fmt} {tf}".strip())
                return t.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
    raise ValueError(f"unreadable date {s!r} — export dates as YYYY-MM-DD")


def derive_ref(rec, prefix="CSV"):
    """A stable id for an export with no order number.

    Stable is the whole requirement. The same row in the same file dropped
    twice must produce the same entity_id, or EventLog.append cannot recognise
    it as a replay and the order is counted twice. So: a hash of the fields a
    human would use to say "that one" — who, what, when — and nothing that
    varies per upload (no timestamp, no row number, no uuid)."""
    key = "|".join(str(rec.get(k) or "") for k in
                   ("customer", "product_name", "style_no", "colour", "size",
                    "quantity", "ordered_at"))
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:10].upper()}"


def parse_csv(blob, day_first=DAY_FIRST, ref_prefix="CSV"):
    """Bytes or text of a CSV -> (records, refusals, meta).

    records  ready for orders.replay
    refusals [{line, reason, raw}] — the work list
    meta     {encoding, delimiter, rows, accepted, refused, unmapped_columns,
              has_price_column}

    `rows == accepted + refused` is asserted before returning. It is cheap and
    it is the one property that makes the counts printed by a run trustworthy.
    """
    text, enc = decode(blob)
    text = text.replace("\r\n", "\n")
    head = text.split("\n", 1)[0]
    dialect = _dialect(text[:4096])
    reader = csv.reader(io.StringIO(text), dialect)

    try:
        header = next(reader)
    except StopIteration as e:
        raise CsvHeaderError("file is empty") from e
    cols, unmapped = map_header(header)

    records, refusals, rows = [], [], 0
    for lineno, row in enumerate(reader, start=2):
        if not any((c or "").strip() for c in row):
            continue                      # trailing blank line from Excel
        rows += 1
        raw = {f: (row[i] if i < len(row) else "") for f, i in cols.items()}
        try:
            rec = {
                "customer": raw["customer"].strip(),
                "product_name": raw["product_name"].strip(),
                "quantity": quantity(raw["quantity"]),
                "ordered_at": when(raw["ordered_at"], day_first),
                "ordered_at_source": raw["ordered_at"].strip(),
            }
            for f in ("platform", "style_no", "colour", "size", "product_url",
                      "hts_code", "waybill"):
                if f in cols:
                    rec[f] = raw[f].strip() or None
            for f in NUMERIC:
                if f in cols:
                    rec[f] = money(raw[f])
            missing = [k for k in CSV_REQUIRED if not rec.get(k)]
            if missing:
                raise ValueError(f"empty {missing}")
            rec["order_ref"] = (raw.get("order_ref", "").strip()
                                or derive_ref(rec, ref_prefix))
            rec["order_ref_derived"] = not raw.get("order_ref", "").strip()
            records.append(rec)
        except (ValueError, TypeError, KeyError) as e:
            refusals.append({"line": lineno, "reason": str(e),
                             "raw": ",".join(str(c) for c in row)[:160]})

    dupes = _duplicate_refs(records)
    assert rows == len(records) + len(refusals), (
        f"conservation broken: {rows} rows != {len(records)} + {len(refusals)}")
    return records, refusals, {
        "encoding": enc,
        "delimiter": getattr(dialect, "delimiter", ","),
        "header": head[:200],
        "rows": rows,
        "accepted": len(records),
        "refused": len(refusals),
        "unmapped_columns": unmapped,
        "has_price_column": "retail_price_cad" in cols,
        "duplicate_refs": dupes,
    }


def _duplicate_refs(records):
    """Two rows claiming the same order_ref. Reported, not refused: the second
    one will land on the same entity in the ledger, which is either a genuine
    correction or a copy-paste mistake, and only a human can tell which."""
    seen, dupes = {}, []
    for r in records:
        seen.setdefault(r["order_ref"], []).append(r.get("customer"))
    for ref, who in seen.items():
        if len(who) > 1:
            dupes.append({"order_ref": ref, "count": len(who), "customers": who})
    return dupes


def load_fixture(name="orders_sample.csv"):
    path = os.path.join(os.path.dirname(__file__), "..", "fixtures", name)
    with open(path, "rb") as f:
        return parse_csv(f.read())


# ── the return leg ───────────────────────────────────────────────────────────
#
# A pipeline that swallows a spreadsheet and answers "16 events appended" has
# not helped anybody. What comes back has to be the thing the work needs next:
# a row per order with the price to quote, the rate it was computed at, and —
# for the ones that did not price — the reason, in the same file, so nothing
# has to be chased through a log.

RESULT_COLUMNS = [
    "order_ref", "status", "customer", "platform", "product_name",
    "style_no", "colour", "size", "quantity",
    "retail_price_cad", "landed_cad",
    "fx_rate", "fx_effective_from", "pricing_version",
    "sell_cny", "total_cny", "profit_cad", "capped_by_china_price",
    "reason", "order_ref_derived", "ordered_at", "ordered_at_source",
    "customer_message",
]


def result_rows(records, pricing=None, rates=None):
    """[record] -> [dict] in RESULT_COLUMNS order, one row per accepted record.

    `status` is `quoted` or `unpriced`; a refused row never got this far and
    lives in the refusals list instead, which is why the writer below takes
    both."""
    from . import orders as _orders
    pricing = pricing or _orders.Pricing()
    rates = rates if rates is not None else _orders.load_fx()
    out = []
    for r in records:
        row = {k: r.get(k) for k in
               ("order_ref", "customer", "platform", "product_name", "style_no",
                "colour", "size", "quantity", "retail_price_cad",
                "order_ref_derived", "ordered_at", "ordered_at_source")}
        try:
            q = _orders.quote(r, pricing, rates)
        except (OrderError, TypeError, ValueError) as e:
            out.append({**row, "status": "unpriced", "reason": str(e)})
            continue
        out.append({**row, "status": "quoted", "reason": "",
                    "landed_cad": q.landed_cad,
                    "fx_rate": q.fx_rate,
                    "fx_effective_from": q.fx_effective_from,
                    "pricing_version": q.pricing_version,
                    "sell_cny": q.sell_cny,
                    "total_cny": q.total_cny,
                    "profit_cad": q.profit_cad,
                    "capped_by_china_price": q.capped_by_china_price,
                    "customer_message": _orders.message_line(r, q)})
    return out


def write_results(path, records, refusals, pricing=None, rates=None):
    """Write the priced sheet. UTF-8 with a BOM, because the person opening it
    opens it in Excel, and Excel without a BOM renders every Chinese name as
    mojibake — which makes a correct file look like a broken one.

    Refused rows are appended with status `refused` so the output accounts for
    every row of the input. A file that quietly returns 98 rows for 100 is how
    two customers get forgotten."""
    rows = result_rows(records, pricing, rates)
    for ref in refusals:
        rows.append({"order_ref": f"(line {ref['line']})", "status": "refused",
                     "reason": ref["reason"], "customer_message": ""})
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row.get(k))
                        for k in RESULT_COLUMNS})
    return rows
