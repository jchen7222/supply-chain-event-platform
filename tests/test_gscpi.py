"""Fourth source: the .xls-in-.xlsx trap (confirmed live) and revision memory."""
import os

from ingest import gscpi
from ingest.envelope import EventLog

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
RAW = open(os.path.join(FIX, "gscpi_data.xlsx"), "rb").read()


def test_file_is_ole2_xls_despite_xlsx_name():
    assert RAW[:4] == gscpi.OLE2_MAGIC, "the NY Fed file is legacy .xls in .xlsx clothing"


def test_parse_full_history():
    rows = gscpi.parse(RAW)
    assert len(rows) >= 300
    periods = [p for p, _ in rows]
    assert periods[0].startswith("1998")


def test_snapshot_then_silent_then_revision_then_revert():
    log = EventLog()
    n1, vals = gscpi.snapshot(log, "2026-08-01", RAW)
    assert n1 == len(vals) >= 300
    n2, vals2 = gscpi.snapshot(log, "2026-09-01", RAW, prior_values=vals)
    assert n2 == 0, "same file, no events"
    # a real revision arrives (the NY Fed re-estimates history every release)
    assert log.append("index_value_observed", "gscpi", "GSCPI:2020-04-01",
                      {"series": "GSCPI", "observation_date": "2020-04-01",
                       "value": 9.999},
                      event_time="2020-04-01", record_time="2026-09-15")
    vals2["2020-04-01"] = 9.999
    # next month's file carries the original value again: the REVERT must land
    n3, _ = gscpi.snapshot(log, "2026-10-01", RAW, prior_values=vals2)
    assert n3 == 1, "revert to a prior value is a change and must append"

