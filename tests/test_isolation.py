"""The test suite must not modify the repository's own collected data.

This file exists because it once did. `pytest` deleted data/event_log.jsonl,
data/state.json and data/landing/ and rebuilt them from fixtures, leaving the
working tree dirty with a month of real observations replaced by fixture rows.
Nothing failed, nothing warned — the damage was visible only in `git status`,
and one `git commit -a` would have made it permanent.

So the fix gets a test. These assertions are cheap and they fail loudly the
moment someone points a fixture back at data/ again.
"""
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
TRACKED = ("data/event_log.jsonl", "data/state.json")


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT,
                          capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def status_before():
    return _git("status", "--porcelain", "--", "data")


def test_the_build_writes_somewhere_that_is_not_the_repo(built):
    """The workspace must be outside the working tree. If it is not, every
    other assertion in this file is meaningless."""
    assert os.path.isabs(built["data"])
    assert not os.path.realpath(built["data"]).startswith(os.path.realpath(ROOT)), \
        f"the test warehouse landed inside the repo at {built['data']}"


def test_the_build_actually_produced_a_warehouse_there(built):
    """Guard against the opposite failure: isolation achieved by not building."""
    assert os.path.exists(built["db"]), "no duckdb file in the temp workspace"
    assert os.path.exists(os.path.join(built["data"], "event_log.jsonl"))
    assert os.path.getsize(built["db"]) > 0


def test_the_real_event_log_is_untouched_by_a_test_run(built, status_before):
    """The headline. `git status` on data/ must read the same after the session
    fixture has run as it did before."""
    after = _git("status", "--porcelain", "--", "data")
    assert after == status_before, (
        "a test run changed data/:\n" + after)


@pytest.mark.parametrize("path", TRACKED)
def test_no_tracked_data_file_was_deleted(built, path):
    full = os.path.join(ROOT, path)
    if _git("ls-files", "--", path).strip():
        assert os.path.exists(full), f"{path} is tracked but the tests removed it"


def test_the_landing_directory_still_holds_its_committed_snapshots(built):
    """`shutil.rmtree(data/landing)` was the line that deleted three of these."""
    landed = _git("ls-files", "--", "data/landing").split()
    for rel in landed:
        assert os.path.exists(os.path.join(ROOT, rel)), \
            f"{rel} is committed but no longer on disk"
