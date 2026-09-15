"""Lane 5 — USITC Harmonized Tariff Schedule: duty rates as effective-dated
reference data, with the archive as its own oracle.

WHY THIS SOURCE EXISTS. Every other source here answers "what was observed?".
This one answers "what was the RULE?" — and a rule is the thing most systems
get wrong, because a tariff table is almost always stored as a lookup that gets
overwritten when rates change. Overwrite it once and every past landed cost
silently re-prices under today's rates. Recomputing a 2024 shipment must use
the 2024 rate.

WHAT MAKES IT A GOOD ORACLE. The USITC publishes the HTS as ~249 dated
revisions going back to 1989, each downloadable as JSON or CSV and each citing
the Federal Register notice that caused it. That is a genuine vintage archive:
the revision in force on a date is a fact nobody in this repository can alter.
So the same source supplies the dimension AND the assertion — ingest the
revisions as effective-dated rows, then assert that the rate the warehouse
applied to a past shipment equals the rate in the revision in force that day.
ALFRED proves the bitemporal machinery on a macro series; HTS proves it on the
reference data this platform actually reasons about.

THE PARSE IS NOT TRIVIAL, AND PRETENDING IT IS WOULD BE THE BUG. The `general`
column is a string, and only some of them are ad valorem:

    "Free"            -> 0.0
    "3.4%"            -> 0.034
    "2.5 cents/kg"    -> NOT ad valorem; a specific duty, needs quantity
    "4.4% + 2.5c/kg"  -> compound; needs both

A specific or compound rate cannot be multiplied by a declared value, so it is
flagged rather than coerced to a number. Coercing it would produce a plausible
wrong number, which is worse than a refusal.

Fixtures here are shaped per the USITC JSON export (htsno / description /
general / special / other) and labelled as fixtures — they are not a copy of
the real archive."""
import json
import os
import re

from .client import ResilientClient

_client = ResilientClient("usitc_hts", rate_limit_s=1.0)

BASE = "https://hts.usitc.gov/reststop/exportList"
FIX_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

AD_VALOREM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
FREE = re.compile(r"^\s*free\s*$", re.I)


def fetch_revision_live(revision, fmt="JSON"):
    """One archived revision. `revision` is the USITC edition label, e.g.
    '2024 Revision 5'. Kept behind ResilientClient like every other lane."""
    url = f"{BASE}?from=0100&to=9999&format={fmt}&styles=false&revision={revision}"
    return json.loads(_client.get(url))


def load_revision_fixture(name):
    with open(os.path.join(FIX_DIR, name)) as f:
        return json.load(f)


def load_revisions(index_name="hts_revisions.json"):
    """[(effective_from, label, payload)] ascending, from the fixture index."""
    with open(os.path.join(FIX_DIR, index_name)) as f:
        idx = json.load(f)
    return sorted(((r["effective_from"], r["label"], load_revision_fixture(r["file"]))
                   for r in idx["revisions"]), key=lambda r: r[0])


def parse_rate(raw):
    """Return (rate, kind). `rate` is an ad valorem fraction, or None when the
    duty cannot be expressed as one.

        kind == "ad_valorem"  -> multiply by declared value
        kind == "free"        -> 0.0
        kind == "specific"    -> needs quantity; rate is None
        kind == "compound"    -> needs both; rate is None
        kind == "unparsed"    -> we do not understand it; rate is None
    """
    if raw is None:
        return None, "unparsed"
    s = str(raw).strip()
    if not s:
        return None, "unparsed"
    if FREE.match(s):
        return 0.0, "free"
    m = AD_VALOREM.match(s)
    if m:
        return round(float(m.group(1)) / 100.0, 6), "ad_valorem"
    if "+" in s:
        return None, "compound"
    # anything with a currency/quantity unit is a specific duty
    if re.search(r"(cents?|¢|\$|/\s*(kg|kilo|liter|litre|doz|m2|unit))", s, re.I):
        return None, "specific"
    return None, "unparsed"


def rows(payload):
    """(hts_code, description, general_rate_string) for every line that states a
    rate. Header lines in the HTS carry a description and no rate; they are
    structure, not tariff, and are skipped."""
    out = []
    for r in payload:
        code = (r.get("htsno") or "").strip()
        general = r.get("general")
        if not code or general in (None, ""):
            continue
        out.append((code, (r.get("description") or "").strip(), general))
    return out


def replay(log, revisions):
    """Emit one `duty_rate_observed` event per (hts_code, revision).

    event_time  = the revision's effective date — when the rate became true
    record_time = the same date — when a filer could have known it

    The envelope's idempotency does the rest: a code whose rate did not change
    between two revisions emits nothing the second time, so the log holds
    exactly the CHANGES, and the fold as-of any date reconstructs the whole
    schedule in force that day.

    `revisions` is [(effective_from, label, payload)], ascending.

    NOTE ON WHAT IS *NOT* IN THE PAYLOAD. The revision label is deliberately
    absent. Idempotency here is a hash of the payload, so putting the label in
    it would make every code look "changed" at every revision — 249 revisions
    x ~19,000 lines, and the log would record no-ops instead of changes. The
    label is 1:1 with the effective date, which IS on the event, so nothing is
    lost: `revision_label(revisions, event_time)` recovers it, and the fixture
    index carries the Federal Register citation. Provenance that can be derived
    does not belong in the hashed fact.
    """
    n = 0
    for effective_from, label, payload in sorted(revisions, key=lambda r: r[0]):
        for code, description, general in rows(payload):
            rate, kind = parse_rate(general)
            if log.append(
                    "duty_rate_observed", "usitc_hts", f"HTS:{code}",
                    {"hts_code": code,
                     "description": description,
                     "general_rate_raw": general,
                     "ad_valorem_rate": rate,
                     "rate_kind": kind},
                    event_time=effective_from, record_time=effective_from):
                n += 1
    return n


def revision_label(revisions, effective_from):
    """The revision (and its Federal Register citation) for an effective date —
    provenance recovered from the event rather than duplicated into it."""
    for eff, label, _ in revisions:
        if eff == effective_from:
            return label
    return None


def rate_as_of(revisions, hts_code, on_date):
    """THE ORACLE. What the archive itself says the rate was on `on_date` —
    read straight from the latest revision whose effective date is on or before
    that date. Computed without touching the event log, which is what makes it
    an independent check of the fold rather than a restatement of it."""
    in_force = [r for r in sorted(revisions, key=lambda r: r[0])
                if r[0] <= on_date]
    if not in_force:
        return None
    _, _, payload = in_force[-1]
    for code, _description, general in rows(payload):
        if code == hts_code:
            return parse_rate(general)[0]
    return None


def landed_duty(declared_value_usd, rate):
    """Duty on a shipment. Refuses rather than guesses when the rate is not ad
    valorem — a specific duty needs a quantity this platform does not hold."""
    if rate is None:
        raise ValueError("duty is not ad valorem; a quantity is required")
    return round(declared_value_usd * rate, 2)
