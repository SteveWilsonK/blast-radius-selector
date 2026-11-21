"""Fold the recorded reads of class-level values into their own small file.

Usage: build_reads.py tests.json trace_dir out_file
Kept apart from the main map so that a new trace of reads does not mean a new copy
of the whole map in the repository. Keyed by test id, like the map.
"""
import collections
import json
import pickle
import sys
import zlib

tests = json.load(open(sys.argv[1]))["tests"]
trace_dir, out_file = sys.argv[2], sys.argv[3]
ids = [t["id"] for t in tests]
NBYTES = (len(ids) + 7) // 8
reads = collections.defaultdict(int)
tracked, untracked, shared = set(), set(), {}
for i, test in enumerate(tests):
    for raw in open(f"{trace_dir}/trace-{test['number']}.jsonl"):
        rec = json.loads(raw)
        if rec["phase"] == "import":
            tracked.update(rec.get("tracked") or ())
            untracked.update(rec.get("untracked") or ())
            shared.update(rec.get("shared") or {})
        else:
            bit = 1 << i
            for tag in rec.get("reads") or ():
                reads[tag] |= bit
sets, table = {}, []


def intern(mask):
    key = sets.get(mask)
    if key is None:
        key = sets[mask] = len(table)
        table.append(mask.to_bytes(NBYTES, "little"))
    return key


data = {"version": 3, "ids": ids, "reads": {tag: intern(mask) for tag, mask in reads.items()},
        "sets": table, "tracked": sorted(tracked - untracked), "shared": shared}
blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
open(out_file, "wb").write(blob)
print(f"{len(reads)} read tags, {len(table)} sets, {len(tracked - untracked)} recorded values, "
      f"{len(blob) / 1e6:.2f} MB", file=sys.stderr)
