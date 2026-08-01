"""One client pattern for three real public APIs, three different shapes —
key-optional REST (FRED/ALFRED), rate-limited JSON (openFDA), SDMX (IMF) —
plus a binary download (NY Fed). The same pattern for every source:
retry with exponential backoff, rate-limit pacing, response caching,
pagination handling, and a per-source circuit breaker.

CI never calls a live API (fixtures only); the scheduled ingest workflow
exercises the live paths so integration rot is caught by the badge, not by
a reviewer."""
import hashlib
import json
import os
import time
import urllib.request


class CircuitOpen(RuntimeError):
    pass


class ResilientClient:
    def __init__(self, source, transport=None, max_retries=3, backoff_s=0.5,
                 rate_limit_s=0.6, cache_dir=None, fail_threshold=3,
                 sleep_fn=time.sleep):
        self.source = source
        self.transport = transport or self._urllib_get
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.rate_limit_s = rate_limit_s
        self.cache_dir = cache_dir
        self.fail_threshold = fail_threshold
        self.sleep_fn = sleep_fn
        self.failures = 0
        self.open = False
        self.calls = 0
        self._last = 0.0

    @staticmethod
    def _urllib_get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "manifest/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def _cache_path(self, url):
        h = hashlib.sha256(url.encode()).hexdigest()[:20]
        return os.path.join(self.cache_dir, f"{self.source}_{h}")

    def get(self, url):
        if self.cache_dir:
            p = self._cache_path(url)
            if os.path.exists(p):
                return open(p, "rb").read()
        if self.open:
            raise CircuitOpen(f"{self.source} circuit open")
        wait = self.rate_limit_s - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep_fn(wait)
        err = None
        for attempt in range(self.max_retries):
            try:
                self._last = time.monotonic()
                self.calls += 1
                body = self.transport(url)
                self.failures = 0
                if self.cache_dir:
                    os.makedirs(self.cache_dir, exist_ok=True)
                    with open(self._cache_path(url), "wb") as f:
                        f.write(body)
                return body
            except Exception as e:                          # noqa: BLE001
                err = e
                self.sleep_fn(self.backoff_s * (2 ** attempt))
        self.failures += 1
        if self.failures >= self.fail_threshold:
            self.open = True
        raise err


def paginate_openfda(client, base_url, page_size=1000, max_records=5000):
    """openFDA skip/limit pagination — a generator over result records.
    Stops on an empty page or the max_records guard (never trust an API to
    end pagination for you)."""
    skip = 0
    while skip < max_records:
        sep = "&" if "?" in base_url else "?"
        page = json.loads(client.get(f"{base_url}{sep}limit={page_size}&skip={skip}"))
        results = page.get("results", [])
        if not results:
            return
        yield from results
        if len(results) < page_size:
            return
        skip += page_size
