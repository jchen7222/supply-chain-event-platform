"""Lane 1 — FRED/ALFRED vintage replay: the point-in-time fold, with a test.

ALFRED stores what each series said on every past date (vintages) and is the
ONLY source here with a real vintage API — Census, BLS, and openFDA all serve
current state only. That asymmetry is the argument for this platform.

Live fetches need a free API key (FRED_API_KEY env var); a keyless call
returns HTTP 400. The replay/fold logic is identical for the live payload and
the documentation-shaped fixture in fixtures/alfred_mnfctrirsa_vintages.json
(the fixture is labeled: shaped per the FRED docs, not real archive data)."""
import json
import os
import re

from .client import ResilientClient

_client = ResilientClient("alfred", rate_limit_s=1.0)

SERIES = "MNFCTRIRSA"     # Manufacturers: Inventories to Sales Ratio
BASE = "https://api.stlouisfed.org/fred/series/observations"


def fetch_vintages_live(series=SERIES):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY not set — live ALFRED fetch needs a free key")
    url = (f"{BASE}?series_id={series}&api_key={key}&file_type=json"
           f"&output_type=2&realtime_start=2013-01-01")
    return json.loads(_client.get(url))


def vintage_columns(payload, series=SERIES):
    """output_type=2: one row per observation date, one column per vintage,
    named {SERIES}_{YYYYMMDD}. Returns sorted vintage dates and the matrix."""
    pat = re.compile(rf"^{series}_(\d{{8}})$")
    vintages = set()
    rows = payload["observations"]
    for row in rows:
        for k in row:
            m = pat.match(k)
            if m:
                vintages.add(m.group(1))
    vdates = sorted(f"{v[0:4]}-{v[4:6]}-{v[6:8]}" for v in vintages)
    return vdates, rows


def replay(log, payload, series=SERIES):
    """Turn the vintage archive into events: for each vintage (ascending), any
    observation whose value differs from the previous vintage becomes an event
    with record_time = the vintage date and event_time = the observation period."""
    vdates, rows = vintage_columns(payload, series)
    prev = {}
    n = 0
    for vd in vdates:
        col = f"{series}_{vd.replace('-', '')}"
        for row in rows:
            val = row.get(col)
            if val in (None, "", "."):
                continue
            obs = row["date"]
            if prev.get(obs) == val:
                continue
            prev[obs] = val
            if log.append("series_value_observed", "alfred", f"{series}:{obs}",
                          {"series": series, "observation_date": obs, "value": val,
                           "vintage": vd},
                          event_time=obs, record_time=vd):
                n += 1
    return n, vdates


def vintage_answer(payload, vintage_date, series=SERIES):
    """What ALFRED itself says the series looked like as of one vintage —
    the external oracle the point-in-time fold is asserted against in CI."""
    col = f"{series}_{vintage_date.replace('-', '')}"
    out = {}
    for row in payload["observations"]:
        val = row.get(col)
        if val not in (None, "", "."):
            out[f"{series}:{row['date']}"] = val
    return out
