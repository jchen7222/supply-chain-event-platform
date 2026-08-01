"""The folds THROUGH dbt: current-state uniqueness, history chain, and the
point-in-time model reproducing an ALFRED vintage — the resume-line claim,
executed literally ('folds in dbt, validated in CI against the vintage archive')."""
import json
import os

import duckdb

from ingest import alfred

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "alfred_mnfctrirsa_vintages.json")


def test_current_state_one_row_per_entity(con):
    dup = con.execute("""select entity_id, count(*) c from current_state
                         group by 1 having count(*) > 1""").fetchall()
    assert dup == []


def test_history_valid_from_to_chain(con):
    bad = con.execute("""select count(*) from history
                         where valid_to is not null and valid_to < valid_from""").fetchone()[0]
    assert bad == 0


def test_registry_generated_model_covers_all_sources(con):
    gen = dict((r[0], r[1]) for r in
               con.execute("select source, events from source_event_counts").fetchall())
    raw = dict((r[0], r[1]) for r in
               con.execute("""select source, count(*) from raw.event_log
                              where not is_error_declaration group by 1""").fetchall())
    assert gen == raw


def test_point_in_time_in_dbt_matches_vintage_archive(built):
    payload = json.load(open(FIX))
    vdates, _ = alfred.vintage_columns(payload)
    vd = vdates[1]                     # middle vintage: revisions + a late add
    r = built["dbt"]("--select", "point_in_time", "--vars", f"{{as_of: '{vd}'}}")
    assert r.returncode == 0, r.stdout[-1500:]
    con = duckdb.connect(built["db"], read_only=True)
    got = dict(con.execute("""
        select entity_id, json_extract_string(payload, '$.value')
        from point_in_time where source = 'alfred'""").fetchall())
    con.close()
    assert got == alfred.vintage_answer(payload, vd)
    # restore the default build so other runs see current state
    built["dbt"]("--select", "point_in_time")
