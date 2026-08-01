"""EPCIS envelope semantics: append-only, ErrorDeclaration, as-of-aware folds."""
from ingest.envelope import EventLog


def make_log():
    log = EventLog()
    e1 = log.append("obs", "test", "E1", {"v": 1}, "2026-01-01", "2026-01-02")
    log.append("obs", "test", "E1", {"v": 2}, "2026-01-01", "2026-01-05")
    log.append("obs", "test", "E2", {"v": 9}, "2026-01-01", "2026-01-03")
    return log, e1


def test_same_event_id_only_via_error_declaration():
    log, e1 = make_log()
    ids = [e["event_id"] for e in log.events if not e["is_error_declaration"]]
    assert len(ids) == len(set(ids))
    log.declare_error(e1, "2026-01-06", "wrong value", [])
    dups = [e for e in log.events if e["event_id"] == e1]
    assert len(dups) == 2 and dups[1]["is_error_declaration"]
    assert dups[0]["payload"] == dups[1]["payload"]      # original untouched


def test_fold_current_and_rescission():
    log, e1 = make_log()
    assert log.fold_current()["E1"]["payload_hash"] == log.events[1]["payload_hash"]
    # rescind the LATER event; state falls back to the earlier one
    later_id = log.events[1]["event_id"]
    log.declare_error(later_id, "2026-01-06", "bad revision", [])
    assert log.fold_current()["E1"]["event_id"] == e1


def test_point_in_time_is_as_of_aware():
    log, e1 = make_log()
    later_id = log.events[1]["event_id"]
    log.declare_error(later_id, "2026-01-06", "bad revision", [])
    # BEFORE the declaration was recorded, the (bad) revision was the truth
    assert log.fold_current(as_of="2026-01-05")["E1"]["event_id"] == later_id
    # after it, the original is
    assert log.fold_current(as_of="2026-01-07")["E1"]["event_id"] == e1
