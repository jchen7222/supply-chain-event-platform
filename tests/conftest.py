"""Session fixture: wipe data/, run the fixture ingest, run dbt build once."""
import os
import shutil
import subprocess
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "manifest.duckdb")


def _dbt(*extra):
    return subprocess.run(
        ["dbt", "build", "--profiles-dir", "profiles", *extra],
        cwd=os.path.join(ROOT, "dbt"), capture_output=True, text=True)


@pytest.fixture(scope="session")
def built():
    for p in ("event_log.jsonl", "state.json", "manifest.duckdb"):
        fp = os.path.join(DATA, p)
        if os.path.exists(fp):
            os.remove(fp)
    shutil.rmtree(os.path.join(DATA, "landing"), ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "ingest.run_snapshot",
                        "--fixtures", "--as-of", "2026-08-01"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = _dbt()
    assert d.returncode == 0, d.stdout[-2000:]
    return {"root": ROOT, "db": DB, "dbt": _dbt}


@pytest.fixture()
def con(built):
    c = duckdb.connect(built["db"], read_only=True)
    yield c
    c.close()
