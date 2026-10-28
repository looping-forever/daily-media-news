#!/usr/bin/env python3
"""reduce_candidates.py -- a smaller candidates.json for ONE retry, nothing else.

WHY THIS EXISTS
---------------
Measured: the digest agent ran 600s on a full candidate set and wrote no
`state/stories.json`, while a smaller input finished in 23.7s. The constraint is
prompt/input size, not the model's ability, so the useful second attempt is the
SAME model on a SMALLER input. Asking a different model with the same oversized
input repeats the failure and spends the shared pool.

WHAT IT DOES NOT DO
-------------------
It does not touch the normal path. `state/candidates.json` is written by
collection and never rewritten here; the reduced file is a separate output that
only the bounded retry exports as `CANDIDATES_PATH`. Every top-level key is
carried over unchanged, so the agent still sees weather, source health and the
run's metadata -- only `candidates` is thinned.

HOW IT THINS
------------
Round-robin across `domain_category`, so a category with one candidate is still
represented while a category with twenty is cut to the cap. Straight truncation
would drop whole categories, which reads to the agent as "no local news today"
and produces a wrong digest rather than a shorter one.
"""

import argparse
import json
import os
import sys

DEFAULT_PER_CATEGORY = 1


def _category(cand):
    cat = str(cand.get("domain_category") or "").strip()
    return cat or "uncategorised"


def reduce_candidates(payload, per_category=DEFAULT_PER_CATEGORY):
    """Return a copy of `payload` with at most `per_category` per category."""
    if not isinstance(payload, dict):
        raise ValueError("candidates payload must be a JSON object")
    cands = payload.get("candidates")
    if not isinstance(cands, list):
        raise ValueError("candidates payload has no 'candidates' list")

    buckets = {}
    for idx, cand in enumerate(cands):
        if isinstance(cand, dict):
            buckets.setdefault(_category(cand), []).append((idx, cand))

    kept = []
    # Round-robin by rank, then restore the collector's own order at the end, so
    # the thinned set keeps the ranking it arrived with instead of alphabetising.
    for rank in range(max(0, per_category)):
        for cat in sorted(buckets, key=lambda c: buckets[c][0][0]):
            rows = buckets[cat]
            if rank < len(rows):
                kept.append(rows[rank])
    kept.sort(key=lambda pair: pair[0])

    out = dict(payload)
    out["candidates"] = [cand for _idx, cand in kept]
    out["candidates_reduced_from"] = len(cands)
    out["candidates_per_category"] = per_category
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", default="state/candidates.json")
    ap.add_argument("--out", dest="out_path",
                    default="state/candidates_limited.json")
    ap.add_argument("--per-category", dest="per_category", type=int,
                    default=DEFAULT_PER_CATEGORY)
    args = ap.parse_args(argv)

    with open(args.in_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    before = len(payload.get("candidates") or [])
    reduced = reduce_candidates(payload, max(1, args.per_category))

    out_dir = os.path.dirname(os.path.abspath(args.out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = args.out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(reduced, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, args.out_path)

    after = len(reduced["candidates"])
    print("reduce_candidates: %d -> %d candidate(s) (max %d per category) -> %s"
          % (before, after, args.per_category, args.out_path))
    # Nothing kept means the retry would be pointless; say so with an exit code
    # the caller can branch on rather than spending a model call on an empty file.
    return 0 if after else 2


if __name__ == "__main__":
    sys.exit(main())
