"""The event envelope, modeled on GS1 EPCIS 2.0 (ISO/IEC 19987:2024).

Four decisions taken from the standard, cited in the README:
  * `event_time` (when it happened in the world) is separated from
    `record_time` (when this repository learned it) — EPCIS's bitemporal split.
  * No mechanism exists to modify or delete an event. Corrections are new
    events whose business meaning amends a prior one.
  * An ErrorDeclaration is the sole case where the same non-null event_id
    appears twice; it carries a declaration_time, an optional reason, and
    corrective_event_ids that point FORWARD to replacement events.
  * The repository assigns record_time; a replay may supply the historical
    record_time explicitly (that is what makes vintage replay possible).

We claim the envelope is modeled on EPCIS — we do not claim EPCIS experience."""
import hashlib
import json
import uuid

COLUMNS = ["seq", "event_id", "event_type", "source", "entity_id",
           "event_time", "record_time", "payload", "payload_hash",
           "is_error_declaration", "declaration_time", "reason",
           "corrective_event_ids"]


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class EventLog:
    """Append-only in-memory log; persists to DuckDB (raw.event_log) and JSONL."""

    def __init__(self):
        self.events = []
        self._latest_hash = {}          # entity_id -> payload_hash of latest event
        self._seen = set()              # exact replay keys: (type, entity, et, rt, hash)

    @classmethod
    def from_jsonl(cls, path):
        import os
        log = cls()
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    ev = json.loads(line)
                    log.events.append(ev)
                    if not ev["is_error_declaration"]:
                        log._latest_hash[ev["entity_id"]] = ev["payload_hash"]
                        log._seen.add((ev["event_type"], ev["entity_id"],
                                       ev["event_time"], ev["record_time"],
                                       ev["payload_hash"]))
        return log

    def append(self, event_type, source, entity_id, payload,
               event_time, record_time, event_id=None):
        """Append one event. Idempotent by natural key: if the entity's latest
        payload hash already equals this payload's hash, the observation is
        unchanged and nothing is appended (returns None). Replaying the same
        fixtures is therefore a no-op — a test, not a hope."""
        h = _hash(payload)
        key = (event_type, entity_id, event_time, record_time, h)
        if key in self._seen:                       # exact replay: no-op
            return None
        if self._latest_hash.get(entity_id) == h:   # unchanged observation: no-op
            # Record it as seen even though nothing was appended. Otherwise a
            # later full replay re-emits this observation once the entity's
            # latest hash has moved on: A -> A -> B replayed twice would append
            # the second A. Deciding "no-op" IS having seen it.
            self._seen.add(key)
            return None
        ev = {
            "seq": len(self.events),
            "event_id": event_id or str(uuid.uuid4()),
            "event_type": event_type, "source": source, "entity_id": entity_id,
            "event_time": event_time, "record_time": record_time,
            "payload": json.dumps(payload, sort_keys=True),
            "payload_hash": h,
            "is_error_declaration": False,
            "declaration_time": None, "reason": None,
            "corrective_event_ids": json.dumps([]),
        }
        self.events.append(ev)
        self._latest_hash[entity_id] = h
        self._seen.add(key)
        return ev["event_id"]

    def declare_error(self, original_event_id, declaration_time, reason,
                      corrective_event_ids=None):
        """EPCIS ErrorDeclaration: same event_id, original left untouched."""
        orig = next(e for e in self.events if e["event_id"] == original_event_id
                    and not e["is_error_declaration"])
        ev = dict(orig)
        ev["seq"] = len(self.events)
        ev["record_time"] = declaration_time
        ev["is_error_declaration"] = True
        ev["declaration_time"] = declaration_time
        ev["reason"] = reason
        ev["corrective_event_ids"] = json.dumps(corrective_event_ids or [])
        self.events.append(ev)
        return original_event_id

    # ---- folds (mirrored 1:1 by the dbt models) ----
    def _visible(self, as_of=None):
        evs = [e for e in self.events
               if as_of is None or e["record_time"] <= as_of]
        rescinded = {e["event_id"] for e in evs if e["is_error_declaration"]}
        return [e for e in evs
                if not e["is_error_declaration"] and e["event_id"] not in rescinded]

    def fold_current(self, as_of=None):
        state = {}
        for e in sorted(self._visible(as_of), key=lambda e: (e["record_time"], e["seq"])):
            state[e["entity_id"]] = e
        return state

    def fold_history(self, entity_id):
        return [e for e in self._visible() if e["entity_id"] == entity_id]

    # ---- persistence ----
    def to_duckdb(self, con, table="raw.event_log"):
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(f"""CREATE TABLE {table} (
            seq BIGINT, event_id VARCHAR, event_type VARCHAR, source VARCHAR,
            entity_id VARCHAR, event_time VARCHAR, record_time VARCHAR,
            payload VARCHAR, payload_hash VARCHAR, is_error_declaration BOOLEAN,
            declaration_time VARCHAR, reason VARCHAR, corrective_event_ids VARCHAR)""")
        con.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [[e[c] for c in COLUMNS] for e in self.events])

    def to_jsonl(self, path):
        with open(path, "w") as f:
            for e in self.events:
                f.write(json.dumps(e) + "\n")
