"""Stage 5 — pharma chain of custody + the recall drill.

Honesty split, stated before you ask: the NDC identifiers and the recall are
REAL (openFDA NDC directory and enforcement records, captured in fixtures/);
serialized movements are NOT public, so the EPCIS-shaped movement stream is
SYNTHETIC (seeded) over those real identifiers. Real methods, real trigger,
labeled synthetic stream.

One lot's final receive is recorded LATE on purpose (record_time hours after
event_time): the as-of drill then shows the system believing the lot was still
at the depot at 06:00 while the current fold shows it at the pharmacy — that
gap is what bitemporality is for."""
import json
import random

SITES = {
    "PLANT-1": "plant", "DEPOT-A": "depot", "DEPOT-B": "depot",
    "PH-01": "pharmacy", "PH-02": "pharmacy", "PH-03": "pharmacy",
    "PH-04": "pharmacy", "PH-05": "pharmacy", "PH-06": "pharmacy",
}


def pick_recall(enforcement_fixture_path):
    """First real recall carrying real NDC links."""
    d = json.load(open(enforcement_fixture_path))
    for r in d["results"]:
        ndcs = r.get("openfda", {}).get("product_ndc") or []
        if ndcs:
            return {"recall_number": r["recall_number"],
                    "reason": r.get("reason_for_recall", ""),
                    "status": r.get("status"),
                    "product": r.get("product_description", "")[:80],
                    "ndcs": ndcs[:3]}
    raise RuntimeError("no NDC-linked recall in fixture")


def generate(log, ndcs, day="2026-07-30", seed=7, lots_per_ndc=3):
    """Synthetic EPCIS-shaped movements: commission -> aggregate -> ship ->
    receive (depot) -> ship -> receive (pharmacy), per lot."""
    rng = random.Random(seed)
    n = 0
    for ndc in ndcs:
        for i in range(lots_per_ndc):
            lot = f"lot:{ndc}:L{i+1:02d}"
            depot = rng.choice(["DEPOT-A", "DEPOT-B"])
            pharm = rng.choice([s for s, k in SITES.items() if k == "pharmacy"])
            t = 6 * 60 + rng.randrange(0, 120)      # commissioning time, minutes
            steps = [
                ("epcis_commission", "PLANT-1", None),
                ("epcis_aggregate",  "PLANT-1", f"case:{ndc}:{i+1}"),
                ("epcis_ship",       "PLANT-1", depot),
                ("epcis_receive",    depot,     None),
                ("epcis_ship",       depot,     pharm),
                ("epcis_receive",    pharm,     None),
            ]
            for k, (etype, site, extra) in enumerate(steps):
                et = f"{day}T{(t + k * 90) // 60:02d}:{(t + k * 90) % 60:02d}"
                # the LAST receive of the first lot of each NDC is learned late:
                late = (etype == "epcis_receive" and site == pharm and i == 0)
                rt = f"{day}T18:30" if late else et
                holder = extra if etype == "epcis_ship" else site
                appended = log.append(etype, "pharma_synthetic", lot, {
                    "ndc": ndc, "lot": lot, "site": site,
                    "to": extra if etype == "epcis_ship" else None,
                    "case_id": extra if etype == "epcis_aggregate" else None,
                    "holder_after": holder, "step": k,
                }, event_time=et, record_time=rt)
                if appended:
                    n += 1
    return n


def drill(log, recall, as_of=None):
    """Every lot of the recalled NDCs: who holds it (current or as-of),
    and its full chain of custody."""
    state = log.fold_current(as_of=as_of)
    rows = []
    for ent, e in sorted(state.items()):
        if e["source"] != "pharma_synthetic":
            continue
        p = json.loads(e["payload"])
        if p["ndc"] in recall["ndcs"]:
            chain = [json.loads(x["payload"])["holder_after"]
                     for x in log.fold_history(ent)]
            rows.append({"lot": ent, "ndc": p["ndc"],
                         "holder": p["holder_after"],
                         "hops": " -> ".join(dict.fromkeys(chain))})
    return rows


def render(recall, rows, label):
    out = [f"RECALL DRILL [{label}] — {recall['recall_number']} "
           f"({recall['status']}): {recall['product']}",
           f"  reason: {recall['reason'][:90]}"]
    for r in rows:
        out.append(f"  {r['lot']:28} held by {r['holder']:8} | {r['hops']}")
    return "\n".join(out)
