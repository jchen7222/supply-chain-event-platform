"""Append-only raw landing: one file per fetch, content-addressed.
Nothing parsed at landing means nothing can break at landing."""
import hashlib
import json
import os


def land(landing_dir, source, name, raw_bytes, fetched_at):
    os.makedirs(os.path.join(landing_dir, source), exist_ok=True)
    digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
    path = os.path.join(landing_dir, source, f"{fetched_at}_{name}_{digest}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(raw_bytes)
    return path


def manifest_of(landing_dir):
    out = []
    for root, _, files in os.walk(landing_dir):
        for fn in sorted(files):
            out.append(os.path.join(root, fn))
    return out
