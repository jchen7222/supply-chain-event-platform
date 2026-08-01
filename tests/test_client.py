"""The API-integration layer: retry/backoff, circuit breaker, cache, pagination."""
import json

import pytest

from ingest.client import CircuitOpen, ResilientClient, paginate_openfda


def test_retry_then_success_with_exponential_backoff():
    calls, sleeps = [], []
    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise TimeoutError("transient")
        return b'{"ok": 1}'
    c = ResilientClient("t", transport=flaky, max_retries=3, backoff_s=0.1,
                        rate_limit_s=0, sleep_fn=sleeps.append)
    assert c.get("http://x") == b'{"ok": 1}'
    assert len(calls) == 3 and sleeps[0] < sleeps[1]


def test_circuit_breaker_opens_and_fails_fast():
    def down(url):
        raise ConnectionError("down")
    c = ResilientClient("t", transport=down, max_retries=1, backoff_s=0,
                        rate_limit_s=0, fail_threshold=2, sleep_fn=lambda s: None)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            c.get("http://x")
    with pytest.raises(CircuitOpen):
        c.get("http://x")
    assert c.calls == 2, "an open circuit spends zero further calls"


def test_response_cache_prevents_second_call(tmp_path):
    calls = []
    def once(url):
        calls.append(url)
        return b"payload"
    c = ResilientClient("t", transport=once, rate_limit_s=0,
                        cache_dir=str(tmp_path), sleep_fn=lambda s: None)
    assert c.get("http://x") == b"payload"
    assert c.get("http://x") == b"payload"
    assert len(calls) == 1


def test_openfda_pagination_walks_pages_and_stops():
    pages = {0: [{"i": k} for k in range(3)], 3: [{"i": 3}]}
    def transport(url):
        skip = int(url.split("skip=")[1])
        return json.dumps({"results": pages.get(skip, [])}).encode()
    c = ResilientClient("t", transport=transport, rate_limit_s=0,
                        sleep_fn=lambda s: None)
    got = list(paginate_openfda(c, "https://api.fda.gov/drug/x.json", page_size=3))
    assert [r["i"] for r in got] == [0, 1, 2, 3]
