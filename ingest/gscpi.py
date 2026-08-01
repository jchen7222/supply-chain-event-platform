"""The fourth source — NY Fed Global Supply Chain Pressure Index.

Two reasons it is last: it is the cheapest possible source to add (if adding
it touches anything but a registry row and this small mapping, the abstraction
is wrong), and it has the most extreme revision behavior here — a principal
component re-estimated over the full history every month, with inputs that
arrive carrying "revisions to up to twelve months of previous data."

Trap, confirmed live: the download is an OLE2 legacy .xls wearing an .xlsx
extension (magic bytes d0cf11e0). openpyxl fails on it; sniff the magic and
pick the engine."""
import io

import pandas as pd

from .client import ResilientClient

_client = ResilientClient("gscpi", rate_limit_s=1.0)

URL = ("https://www.newyorkfed.org/medialibrary/research/interactives/"
       "gscpi/downloads/gscpi_data.xlsx")
OLE2_MAGIC = b"\xd0\xcf\x11\xe0"


def fetch_live():
    return _client.get(URL)


def parse(raw_bytes):
    engine = "xlrd" if raw_bytes[:4] == OLE2_MAGIC else "openpyxl"
    df = pd.read_excel(io.BytesIO(raw_bytes), sheet_name="GSCPI Monthly Data",
                       engine=engine)
    df = df.rename(columns={df.columns[0]: "period", df.columns[1]: "gscpi"})
    df = df.dropna(subset=["gscpi"])
    df = df[pd.to_datetime(df["period"], errors="coerce").notna()]
    df["period"] = pd.to_datetime(df["period"]).dt.strftime("%Y-%m-01")
    return list(df[["period", "gscpi"]].itertuples(index=False, name=None))


def snapshot(log, fetched_at, raw_bytes, prior_values=None):
    """Whole-history snapshot -> events only where the value changed (every
    monthly release re-estimates back to 1998, so revisions are the point)."""
    prior_values = prior_values or {}
    new_values = {}
    n = 0
    for period, value in parse(raw_bytes):
        v = round(float(value), 6)
        new_values[period] = v
        if prior_values.get(period) == v:
            continue
        if log.append("index_value_observed", "gscpi", f"GSCPI:{period}",
                      {"series": "GSCPI", "observation_date": period, "value": v},
                      event_time=period, record_time=fetched_at):
            n += 1
    return n, new_values
