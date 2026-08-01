"""Normalization at ingest, with every change logged into the payload.

Confirmed against live openFDA data (July 2026):
  * status is `Ongoing` — the FDA's own prose page spells it `On-Going`,
    and querying that string returns zero records;
  * production data contains misspelled enums (`Avaliable`, `Unvailable`);
  * sibling endpoints disagree on date formats: enforcement uses YYYYMMDD,
    shortages uses MM/DD/YYYY."""
import re

ENUM_FIXES = {
    "Avaliable": "Available",
    "Unvailable": "Unavailable",
    "On-Going": "Ongoing",
}


def normalize_record(rec: dict, date_fields_ymd=(), date_fields_mdy=()):
    rec = dict(rec)
    notes = []
    for k, v in list(rec.items()):
        if isinstance(v, str) and v in ENUM_FIXES:
            rec[k] = ENUM_FIXES[v]
            notes.append(f"enum:{k}:{v}->{ENUM_FIXES[v]}")
    for k in date_fields_ymd:          # 20151125 -> 2015-11-25
        v = rec.get(k)
        if isinstance(v, str) and re.fullmatch(r"\d{8}", v):
            rec[k] = f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
            notes.append(f"date:{k}:YYYYMMDD->ISO")
    for k in date_fields_mdy:          # 07/20/2026 -> 2026-07-20
        v = rec.get(k)
        if isinstance(v, str) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", v):
            mm, dd, yyyy = v.split("/")
            rec[k] = f"{yyyy}-{mm}-{dd}"
            notes.append(f"date:{k}:MM/DD/YYYY->ISO")
    if notes:
        rec["_normalizations"] = notes
    return rec, notes
