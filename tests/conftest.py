"""Session fixture: ingest the fixtures and build dbt once, in a TEMPORARY
directory.

WHY NOT data/. The previous version of this file deleted data/event_log.jsonl,
data/state.json, data/manifest.duckdb and the whole of data/landing/, then
rebuilt them from fixtures. Three of those are tracked files, and
data/event_log.jsonl is the real collected history the nightly ingest appends
to. So running `pytest` left the working tree dirty with fixture data standing
in for a month of real observations — one `git commit -a` away from destroying
ALFRED vintages that cannot be re-collected, because ALFRED serves the vintage
current on the day you ask and those days have passed.

A test suite must not write into the data its own repository is collecting.

HOW. The run happens in a temp directory, named by two environment variables
that both halves of the pipeline already honour:

    MANIFEST_DATA   where the ingest writes    (ingest/run_snapshot.py)
    MANIFEST_DB     where dbt reads and writes (dbt/profiles/profiles.yml)

Both are passed to the subprocesses only; this process's own environment is
left alone. pytest removes the directory afterwards.

The property this buys is asserted in test_isolation.py: `git status` says the
same thing after a test run as it did before.
"""
import os
import subprocess
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def workspace(tmp_path_factory):
    """The throwaway data directory, and the env that points both tools at it."""
    data = tmp_path_factory.mktemp("manifest-data")
    db = str(data / "manifest.duckdb")
    return {"data": str(data), "db": db,
            "env": {**os.environ, "MANIFEST_DATA": str(data), "MANIFEST_DB": db}}


@pytest.fixture(scope="session")
def built(workspace):
    env = workspace["env"]

    r = subprocess.run([sys.executable, "-m", "ingest.run_snapshot",
                        "--fixtures", "--as-of", "2026-08-01"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, (r.stderr or r.stdout)[-2000:]

    def dbt(*extra):
        return subprocess.run(
            ["dbt", "build", "--profiles-dir", "profiles", *extra],
            cwd=os.path.join(ROOT, "dbt"), capture_output=True, text=True, encoding="utf-8", env=env)

    d = dbt()
    assert d.returncode == 0, d.stdout[-2000:]
    return {"root": ROOT, "db": workspace["db"], "data": workspace["data"],
            "dbt": dbt}


@pytest.fixture()
def con(built):
    c = duckdb.connect(built["db"], read_only=True)
    yield c
    c.close()
