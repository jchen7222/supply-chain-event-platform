"""The spreadsheet boundary, and the property that makes an upload box safe.

The tests that matter most here are not the parsing ones — they are
`test_second_drop_of_the_same_file_appends_nothing` and its neighbours. An
upload box is used by a human who cannot see the ledger, so the only way they
can check whether their file went through is to send it again. If that
duplicates the orders, the feature is worse than no feature.
"""
import csv
import io
import os

import pytest

from ingest import orders, orders_csv as oc
from ingest.envelope import EventLog

RECORD_TIME = "2026-09-17"


# ── header mapping ───────────────────────────────────────────────────────────

def test_chinese_and_english_headers_map_to_the_same_fields():
    cn = "订单号,客户,商品名称,数量,加拿大官网价,下单时间\n" \
         "A1,小美,Align Pant,1,128,2026-09-02\n"
    en = "Order No,Customer Name,Product,Qty,Retail Price (CAD),Order Date\n" \
         "A1,小美,Align Pant,1,128,2026-09-02\n"
    a, _, _ = oc.parse_csv(cn)
    b, _, _ = oc.parse_csv(en)
    assert a == b


def test_header_normalisation_survives_excel_decoration():
    # BOM, a full-width bracket, a trailing space, an underscore.
    assert oc._norm("﻿ Retail_Price（CAD） ") == "retailpricecad"
    assert oc._norm("数 量") == "数量"


def test_missing_required_column_is_a_file_error_not_a_row_error():
    # 100 rows with no date column is one misconfiguration, not 100 work items.
    blob = "订单号,客户,商品名称,数量\n" + "".join(
        f"A{i},x,Align,1\n" for i in range(100))
    with pytest.raises(oc.CsvHeaderError) as e:
        oc.parse_csv(blob)
    assert "ordered_at" in str(e.value)
    assert "下单时间" in str(e.value)     # tells her what to rename it to


def test_unmapped_columns_are_reported_not_dropped_silently():
    blob = ("订单号,客户,商品名称,数量,下单时间,备注,内部编号\n"
            "A1,小美,Align Pant,1,2026-09-02,加急,X99\n")
    _, _, meta = oc.parse_csv(blob)
    assert meta["unmapped_columns"] == ["备注", "内部编号"]


# ── encoding: the one that actually bites ────────────────────────────────────

def test_gb18030_export_from_chinese_windows_excel_is_read():
    text = "订单号,客户,商品名称,数量,加拿大官网价,下单时间\n" \
           "A1,王太太,Define Jacket,1,148,2026-09-04\n"
    recs, _, meta = oc.parse_csv(text.encode("gb18030"))
    assert meta["encoding"] == "gb18030"
    assert recs[0]["customer"] == "王太太"


def test_utf8_bom_does_not_break_the_first_column():
    text = "订单号,客户,商品名称,数量,下单时间\nA1,小美,Align,1,2026-09-02\n"
    recs, _, meta = oc.parse_csv(text.encode("utf-8-sig"))
    assert meta["encoding"] == "utf-8-sig"
    assert recs[0]["order_ref"] == "A1"        # not "﻿A1", and not derived


def test_semicolon_delimited_export_is_read():
    text = "订单号;客户;商品名称;数量;下单时间\nA1;小美;Align Pant;1;2026-09-02\n"
    recs, _, meta = oc.parse_csv(text)
    assert meta["delimiter"] == ";"
    assert recs[0]["product_name"] == "Align Pant"


# ── values ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cell,want", [
    ("C$128.00", 128.0), ("$89.00", 89.0), ("¥1,080", 1080.0),
    ("1,080.50", 1080.5), ("148", 148.0), ("148 CAD", 148.0),
    ("", None), ("  ", None), ("n/a", None), ("—", None),
    ("(12.00)", -12.0),
])
def test_money_cells(cell, want):
    assert oc.money(cell) == want


def test_a_price_that_will_not_parse_is_refused_not_read_as_zero():
    # A price silently becoming 0.00 produces a confident quote at a loss,
    # which is the worst available outcome — worse than no quote.
    with pytest.raises(ValueError):
        oc.money("itemised")
    with pytest.raises(ValueError):
        oc.money("ask me")


@pytest.mark.parametrize("cell,want", [("2", 2), ("2 件", 2), ("2.0", 2), ("3个", 3)])
def test_quantity_cells(cell, want):
    assert oc.quantity(cell) == want


@pytest.mark.parametrize("cell", ["0", "-1", "1.5", "", "一件"])
def test_bad_quantities_refused(cell):
    with pytest.raises(ValueError):
        oc.quantity(cell)


@pytest.mark.parametrize("cell,want", [
    ("2026-09-02", "2026-09-02"),
    ("2026-09-02 09:15", "2026-09-02"),
    ("2026/09/02", "2026-09-02"),
    ("2026年9月3日", "2026-09-03"),
    ("9/2/2026 10:02", "2026-09-02"),      # month-first convention
    ("46269", "2026-09-04"),               # Excel serial
])
def test_date_cells(cell, want):
    assert oc.when(cell).startswith(want)


def test_excel_serial_epoch_is_anchored_to_a_known_value():
    # 45292 = 2024-01-01 is the standard check on the 1900 date system,
    # including Excel's phantom 29 February 1900. Without an anchor, an
    # off-by-one in the epoch shifts every serial-format date by a day, which
    # is enough to pick up the wrong FX rate.
    assert oc.when("45292").startswith("2024-01-01")


def test_a_small_bare_integer_is_not_treated_as_a_date():
    # The serial branch is deliberately narrow (5 digits, 20000-80000). A "1"
    # or a "42" in the date column is a row number somebody pasted into the
    # wrong place, and reading it as a date in 1900 would be worse than
    # refusing the row.
    for cell in ("1", "42", "2026"):
        with pytest.raises(ValueError):
            oc.when(cell)


def test_day_first_flag_flips_the_ambiguous_case_only():
    assert oc.when("9/2/2026", day_first=False).startswith("2026-09-02")
    assert oc.when("9/2/2026", day_first=True).startswith("2026-02-09")
    # 25 cannot be a month under either convention
    assert oc.when("25/9/2026", day_first=False).startswith("2026-09-25")


def test_the_source_date_string_is_kept_verbatim():
    # So a wrong day/month convention is recoverable from the ledger instead of
    # baked into it.
    recs, _, _ = oc.parse_csv(
        "订单号,客户,商品名称,数量,下单时间\nA1,小美,Align,1,9/2/2026\n")
    assert recs[0]["ordered_at_source"] == "9/2/2026"


def test_unreadable_date_refuses_the_row():
    _, refs, meta = oc.parse_csv(
        "订单号,客户,商品名称,数量,下单时间\nA1,小美,Align,1,not sure\n")
    assert meta["accepted"] == 0 and meta["refused"] == 1
    assert "unreadable date" in refs[0]["reason"]
    assert refs[0]["line"] == 2


# ── conservation ─────────────────────────────────────────────────────────────

def test_rows_equal_accepted_plus_refused_on_the_messy_fixture():
    recs, refs, meta = oc.load_fixture()
    assert meta["rows"] == meta["accepted"] + meta["refused"] == len(recs) + len(refs)
    assert meta["rows"] == 10 and meta["accepted"] == 8 and meta["refused"] == 2


def test_one_bad_row_does_not_cost_the_others_their_quotes():
    blob = ("订单号,客户,商品名称,数量,加拿大官网价,下单时间\n"
            "A1,小美,Align,1,128,2026-09-02\n"
            "A2,BAD,Align,0,128,2026-09-02\n"
            "A3,阿岚,Align,1,128,2026-09-02\n")
    recs, refs, meta = oc.parse_csv(blob)
    assert [r["order_ref"] for r in recs] == ["A1", "A3"]
    assert refs[0]["line"] == 3
    assert meta["rows"] == 3


def test_trailing_blank_lines_from_excel_are_not_rows():
    blob = ("订单号,客户,商品名称,数量,下单时间\n"
            "A1,小美,Align,1,2026-09-02\n,,,,\n\n,,,,\n")
    _, _, meta = oc.parse_csv(blob)
    assert meta["rows"] == 1


def test_accepted_equals_quoted_plus_rejected_through_the_pricer():
    recs, _, meta = oc.load_fixture()
    c = orders.replay(EventLog(), recs, record_time=RECORD_TIME)
    assert meta["accepted"] == c["quoted"] + c["rejected"]


# ── the missing price column: placed but not quoted ──────────────────────────

def test_an_export_with_no_price_column_still_records_every_order():
    blob = ("订单号,客户,商品名称,数量,下单时间\n"
            "A1,小美,Align Pant,1,2026-09-02\n"
            "A2,Jenny W,Scuba Hoodie,1,2026-09-02\n")
    recs, _, meta = oc.parse_csv(blob)
    assert meta["has_price_column"] is False
    c = orders.replay(EventLog(), recs, record_time=RECORD_TIME)
    # The customer did ask. Both orders exist; neither can be quoted yet.
    assert c["placed"] == 2 and c["quoted"] == 0 and c["rejected"] == 2


def test_a_missing_price_says_so_in_words_a_human_can_act_on():
    recs, _, _ = oc.parse_csv(
        "订单号,客户,商品名称,数量,下单时间\nA1,小美,Align,1,2026-09-02\n")
    gaps = orders.unquotable(recs)
    assert gaps[0]["reason"] == "no source price supplied — awaiting product lookup"


# ── derived refs and the double drop ─────────────────────────────────────────

def test_a_missing_order_number_is_derived_and_flagged():
    blob = "客户,商品名称,数量,加拿大官网价,下单时间\n小美,Align,1,128,2026-09-02\n"
    recs, _, _ = oc.parse_csv(blob)
    assert recs[0]["order_ref_derived"] is True
    assert recs[0]["order_ref"].startswith("CSV-")


def test_a_derived_ref_is_stable_across_parses():
    blob = "客户,商品名称,数量,加拿大官网价,下单时间\n小美,Align,1,128,2026-09-02\n"
    a, _, _ = oc.parse_csv(blob)
    b, _, _ = oc.parse_csv(blob)
    assert a[0]["order_ref"] == b[0]["order_ref"]


def test_a_derived_ref_distinguishes_two_orders_from_the_same_customer():
    blob = ("客户,商品名称,尺码,数量,加拿大官网价,下单时间\n"
            "小美,Align Pant,8,1,128,2026-09-02\n"
            "小美,Align Pant,10,1,128,2026-09-02\n")
    recs, _, _ = oc.parse_csv(blob)
    assert recs[0]["order_ref"] != recs[1]["order_ref"]


def test_second_drop_of_the_same_file_appends_nothing():
    """THE property. Somebody uploads the file, is unsure it worked, uploads it
    again. The ledger must not grow."""
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    first = orders.replay(log, recs, record_time=RECORD_TIME)
    n = len(log.events)
    assert first["placed"] == 8 and n == 16

    again = orders.replay(log, recs, record_time=RECORD_TIME)
    assert len(log.events) == n
    assert (again["placed"], again["quoted"], again["rejected"]) == (0, 0, 0)


def test_drop_on_a_later_day_also_appends_nothing():
    """The harder case: a different record_time means a different natural key,
    so only the per-(type, entity) hash check catches it."""
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time="2026-09-17")
    n = len(log.events)
    for day in ("2026-09-18", "2026-09-25", "2026-10-01"):
        c = orders.replay(log, recs, record_time=day)
        assert (c["placed"], c["quoted"], c["rejected"]) == (0, 0, 0), day
        assert len(log.events) == n, day


def test_a_corrected_price_in_a_redropped_file_does_land():
    """Idempotence must not become deafness: the same order at a new price is a
    new fact and has to append."""
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time="2026-09-17")
    n = len(log.events)

    fixed = [dict(r, retail_price_cad=138.0) if r["order_ref"] == "A1001" else r
             for r in recs]
    c = orders.replay(log, fixed, record_time="2026-09-18")
    assert (c["placed"], c["quoted"]) == (1, 1)
    assert len(log.events) == n + 2


def test_filling_in_a_missing_price_moves_an_order_from_rejected_to_quoted():
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time="2026-09-17")

    priced = [dict(r, retail_price_cad=98.0) if r["order_ref"] == "A1007" else r
              for r in recs]
    c = orders.replay(log, priced, record_time="2026-09-18")
    assert c["quoted"] == 1 and c["rejected"] == 0
    hist = [e["event_type"] for e in log.fold_history("ORDER:A1007")]
    # The refusal stays in the log. It is how the delay is explained later.
    assert "order_rejected" in hist and "order_quoted" in hist


def test_duplicate_refs_within_one_file_are_reported():
    blob = ("订单号,客户,商品名称,数量,加拿大官网价,下单时间\n"
            "A1,小美,Align,1,128,2026-09-02\n"
            "A1,阿岚,Scuba,1,89,2026-09-03\n")
    _, _, meta = oc.parse_csv(blob)
    assert meta["duplicate_refs"] == [
        {"order_ref": "A1", "count": 2, "customers": ["小美", "阿岚"]}]


# ── the quotes themselves are still right ────────────────────────────────────

def test_a_csv_order_is_quoted_at_its_own_dates_fx_rate():
    rates = orders.load_fx()
    recs, _, _ = oc.load_fixture()
    a = next(r for r in recs if r["order_ref"] == "A1001")
    q = orders.quote(a, orders.Pricing(), rates)
    want_rate, want_eff = orders.fx_as_of(rates, "2026-09-02")
    assert q.fx_rate == want_rate and q.fx_effective_from == want_eff
    assert q.sell_cny > 0 and q.pricing_version == "p1"


def test_the_fixture_exercises_more_than_one_fx_effective_date():
    # Otherwise "priced at its own date's rate" is untested by construction.
    rates = orders.load_fx()
    recs, _, _ = oc.load_fixture()
    effs = {orders.quote(r, orders.Pricing(), rates).fx_effective_from
            for r in recs if r.get("retail_price_cad")}
    assert len(effs) > 1


def test_round_trip_through_csv_writer_is_stable():
    """A file we re-export and re-read must parse to the same records — the
    property that lets the priced results be handed back as a spreadsheet."""
    recs, _, _ = oc.load_fixture()
    buf = io.StringIO()
    cols = ["order_ref", "customer", "product_name", "quantity",
            "retail_price_cad", "ordered_at"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in recs:
        w.writerow([r.get(c) if r.get(c) is not None else "" for c in cols])
    again, refs, _ = oc.parse_csv(buf.getvalue())
    assert not refs
    assert [r["order_ref"] for r in again] == [r["order_ref"] for r in recs]
    assert [r["ordered_at"] for r in again] == [r["ordered_at"] for r in recs]


# ── the persisted ledger, now that it travels over a network ─────────────────

def test_a_truncated_log_line_refuses_the_load_and_names_the_line():
    """New failure mode, new guard. The event log used to be a local file that
    only this code wrote. Once it is fetched and re-uploaded between runs, a
    write cut off mid-line becomes possible — and loading that file must stop,
    not skip. Skipping would replay every event in the damaged line against a
    state that never existed."""
    import tempfile
    from ingest.envelope import EventLog, EventLogCorrupt

    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time=RECORD_TIME)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "event_log.jsonl")
        log.to_jsonl(p)
        assert len(EventLog.from_jsonl(p).events) == 16      # reads back clean

        whole = open(p).read()
        open(p, "w").write(whole[:-40])                      # cut off halfway
        with pytest.raises(EventLogCorrupt) as e:
            EventLog.from_jsonl(p)
        assert "line 16" in str(e.value)


def test_a_line_that_is_json_but_not_an_event_is_refused():
    import tempfile
    from ingest.envelope import EventLog, EventLogCorrupt
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "event_log.jsonl")
        with open(p, "w") as f:
            f.write('{"hello": "world"}\n')
        with pytest.raises(EventLogCorrupt) as e:
            EventLog.from_jsonl(p)
        assert "not an event" in str(e.value)


def test_blank_lines_in_the_log_are_tolerated():
    # A trailing newline is not corruption.
    import tempfile
    from ingest.envelope import EventLog
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time=RECORD_TIME)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "event_log.jsonl")
        log.to_jsonl(p)
        with open(p, "a") as f:
            f.write("\n\n")
        assert len(EventLog.from_jsonl(p).events) == 16


def test_the_log_survives_a_round_trip_through_bytes():
    """What the gateway actually does to it: read as bytes, send, store, send
    back, write. Nothing in that path may change how it loads."""
    import tempfile
    from ingest.envelope import EventLog
    recs, _, _ = oc.load_fixture()
    log = EventLog()
    orders.replay(log, recs, record_time=RECORD_TIME)
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "a.jsonl")
        b = os.path.join(d, "b.jsonl")
        log.to_jsonl(a)
        with open(a, "rb") as f:
            blob = f.read()
        with open(b, "wb") as f:
            f.write(blob)
        reloaded = EventLog.from_jsonl(b)
        assert len(reloaded.events) == 16
        # and it is still idempotent after the round trip
        assert orders.replay(reloaded, recs, record_time=RECORD_TIME) == {
            "placed": 0, "quoted": 0, "rejected": 0, "loss_making": 0}
