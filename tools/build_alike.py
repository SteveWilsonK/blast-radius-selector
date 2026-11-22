"""For each test too costly to run, the cheap tests whose coverage is most like its own.

Usage: build_alike.py tests.json map.bin out_file     (run while the image is built)

A test that cannot be run during selection is judged by the tests that behave most
like it. Likeness is measured on the pinned commit only: the share of sqlglot source
lines two tests both execute, out of the lines either executes (from the coverage
map). Writes {costly test id: [look-alike test ids, most alike first]}.
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import policy  # noqa: E402
from br.maps import Maps  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
maps = Maps(sys.argv[2], tests)
out_file = sys.argv[3]
CHEAP = float(os.environ.get("BR_ALIKE_CHEAP", "0.3"))  # look-alikes must be this cheap to run
KEEP = 8
MIN_LIKENESS = 0.5

id_of = {t["number"]: t["id"] for t in tests}
weight = collections.Counter()
for path, rows in maps.run.items():
    if path.startswith("sqlglot/"):
        for key in rows.values():
            weight[key] += 1
size = collections.Counter()
for key, count in weight.items():
    mask = maps.mask(key)
    while mask:
        low = mask & -mask
        size[low.bit_length() - 1] += count
        mask ^= low
position = {n: i for i, n in enumerate(maps.number) if n is not None}
alike = {}
for number, cost in maps.cost.items():
    if cost < policy.NEVER_RUN or number not in position:
        continue
    bit = position[number]
    shared = collections.Counter()
    for key, count in weight.items():
        mask = maps.mask(key)
        if (mask >> bit) & 1:
            while mask:
                low = mask & -mask
                shared[low.bit_length() - 1] += count
                mask ^= low
    ranked = []
    for other, both in shared.items():
        n = maps.number[other]
        if n is None or n == number or maps.cost.get(n, 1e9) > CHEAP:
            continue
        likeness = both / (size[other] + size[bit] - both)
        if likeness >= MIN_LIKENESS:
            ranked.append((-likeness, id_of[n]))
    ranked.sort()
    alike[id_of[number]] = [name for _, name in ranked[:KEEP]]
json.dump(alike, open(out_file, "w"), indent=0)
print({k: len(v) for k, v in alike.items()}, file=sys.stderr)
