"""Lane 3: real captured IMF data reconciles; the 200-but-empty trap is loud."""
import json
import os

import pytest

from ingest import imf
from ingest.envelope import EventLog

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def _series(name, key):
    return imf.parse_series(open(os.path.join(FIX, name), "rb").read(), key)


def test_zero_series_is_a_hard_failure_not_an_empty_result():
    empty = json.dumps({"data": {"dataSets": [{"series": {}}]}}).encode()
    with pytest.raises(imf.ZeroSeriesError):
        imf.parse_series(empty, "USA..CAN.A")


def test_canada_china_asymmetry_is_real_and_decomposed():
    m_cif = _series("imf_can_m_chn.json", "CAN.MG_CIF_USD.CHN.A")
    m_fob = _series("imf_can_mfob_chn.json", "CAN.MG_FOB_USD.CHN.A")
    x_fob = _series("imf_chn_x_can.json", "CHN.XG_FOB_USD.CAN.A")
    log = EventLog()
    n = imf.asymmetry_events(log, "2026-08-01", m_cif, m_fob, x_fob, "CAN_from_CHN")
    assert n >= 10
    rows = {json.loads(e["payload"])["year"]: json.loads(e["payload"]) for e in log.events}
    y2016 = rows["2016"]
    # two reports of the same physical goods, tens of billions apart
    assert y2016["gap_pct"] > 30
    assert y2016["importer_reported_cif_usd"] > y2016["exporter_reported_fob_usd"]
    # decomposition adds back up
    assert abs(y2016["valuation_component_usd"] + y2016["residual_attribution_usd"]
               - y2016["gap_usd"]) < 1.0
    # valuation (CIF/FOB) is the SMALL part; attribution dominates
    assert abs(y2016["residual_attribution_usd"]) > abs(y2016["valuation_component_usd"])
