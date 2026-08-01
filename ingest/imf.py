"""Lane 3 — bilateral trade asymmetry (IMF IMTS, SDMX 3.0).

Two countries report the same physical shipment and the numbers never match.
IMTS (formerly DOTS — renamed; searching "DOTS" misleads) is free with no key
and carries both MG_CIF_USD and MG_FOB_USD, so the valuation component of the
gap is computable from a single source.

Traps confirmed by testing (July 2026):
  * the legacy endpoint dataservices.imf.org is DNS-dead, docs still online;
  * dimension order is COUNTRY.INDICATOR.COUNTERPART_COUNTRY.FREQUENCY —
    frequency LAST;
  * empty-string wildcards return HTTP 200 with zero series and no error —
    a silent-truncation bug shape; this module treats zero series as a
    hard failure, never as an empty result."""
import json

from .client import ResilientClient

_client = ResilientClient("imf_imts", rate_limit_s=1.0)

BASE = "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/IMTS/+/"


class ZeroSeriesError(RuntimeError):
    """HTTP 200 with no series — the silent wildcard trap, surfaced loudly."""


def fetch_series(key, last_n=15):
    return _client.get(f"{BASE}{key}?lastNObservations={last_n}")


def parse_series(raw_bytes, key):
    """Returns {year: value}. Raises ZeroSeriesError on the 200-but-empty trap."""
    d = json.loads(raw_bytes)
    datasets = d.get("data", {}).get("dataSets", [])
    series = datasets[0].get("series", {}) if datasets else {}
    if not series:
        raise ZeroSeriesError(f"IMF returned HTTP 200 with zero series for {key} "
                              "— check the dimension order (frequency is LAST) "
                              "and use explicit keys, not empty-string wildcards")
    obs_dims = d["data"]["structures"][0]["dimensions"]["observation"]
    periods = [v["value"] for v in obs_dims[0]["values"]]
    out = {}
    for s in series.values():
        for idx, arr in s.get("observations", {}).items():
            val = arr[0]
            if val is not None:
                out[periods[int(idx)]] = float(val)
    return out


def asymmetry_events(log, fetched_at, reporter_imports_cif, reporter_imports_fob,
                     partner_exports_fob, pair, tolerance_pct=5.0):
    """The reconciliation: importer-reported vs exporter-reported flows for the
    same physical goods, decomposed into the valuation (CIF/FOB) component and
    the residual (attribution and everything else)."""
    n = 0
    for year in sorted(set(reporter_imports_cif) & set(partner_exports_fob)):
        m_cif = reporter_imports_cif[year]
        x_fob = partner_exports_fob[year]
        m_fob = reporter_imports_fob.get(year)
        gap = m_cif - x_fob
        gap_pct = 100.0 * gap / m_cif if m_cif else None
        valuation = (m_cif - m_fob) if m_fob is not None else None
        residual = (gap - valuation) if valuation is not None else None
        log.append("trade_mirror_observed", "imf_imts", f"{pair}:{year}", {
            "pair": pair, "year": year,
            "importer_reported_cif_usd": m_cif,
            "importer_reported_fob_usd": m_fob,
            "exporter_reported_fob_usd": x_fob,
            "gap_usd": gap, "gap_pct": round(gap_pct, 2) if gap_pct else None,
            "valuation_component_usd": valuation,
            "residual_attribution_usd": residual,
            "within_tolerance": abs(gap_pct or 0) <= tolerance_pct,
        }, event_time=f"{year}-12-31", record_time=fetched_at)
        n += 1
    return n
