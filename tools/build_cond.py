"""Fold per-test comparison coverage into maps/cond.bin.

Usage: build_cond.py tests.json cond_dir out_file [skipped files, comma separated]
For each comparison site: which tests saw which operand relation.
"""
import collections
import json
import os
import pickle
import sys
import zlib

tests = json.load(open(sys.argv[1]))["tests"]
cond_dir, out_file = sys.argv[2], sys.argv[3]
skipped = sys.argv[4].split(",") if len(sys.argv) > 4 else ["sqlglot/tokenizer_core.py", "sqlglot/trie.py"]
ids = [t["id"] for t in tests]
NBYTES = (len(ids) + 7) // 8

sites = collections.defaultdict(lambda: collections.defaultdict(int))  # site -> code -> mask
missing = 0
for i, test in enumerate(tests):
    path = os.path.join(cond_dir, f"cond-{test['number']}.json")
    if not os.path.exists(path):
        missing += 1
        continue
    bit = 1 << i
    for site, mask in json.load(open(path)).items():
        codes = sites[site]
        code = 0
        while mask:
            if mask & 1:
                codes[code] |= bit
            mask >>= 1
            code += 1

sets, table = {}, []


def intern(mask):
    key = sets.get(mask)
    if key is None:
        key = sets[mask] = len(table)
        table.append(mask.to_bytes(NBYTES, "little"))
    return key


data = {
    "ids": ids,
    "bools": any(site.startswith("bool:") for site in sites),
    "complete": missing == 0,
    "skipped": skipped,
    "sites": {site: {code: intern(mask) for code, mask in codes.items()} for site, codes in sites.items()},
    "sets": table,
}
blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
open(out_file, "wb").write(blob)
print(f"{len(sites)} comparison sites, {len(table)} sets, {missing} tests missing, "
      f"{len(blob) / 1e6:.2f} MB", file=sys.stderr)
