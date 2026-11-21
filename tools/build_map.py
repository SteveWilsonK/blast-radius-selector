"""Fold the per-test traces into the map the selector loads.

Usage: build_map.py tests.json trace_dir out_file [costs.json] [families.json] [self_import.json]
The map is keyed by test id, so it does not depend on tests.json numbering.
"""
import collections
import json
import pickle
import sys
import zlib

tests = json.load(open(sys.argv[1]))["tests"]
trace_dir, out_file = sys.argv[2], sys.argv[3]
costs = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
families = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {}
self_import = json.load(open(sys.argv[6])) if len(sys.argv) > 6 else {}
ids = [t["id"] for t in tests]
NBYTES = (len(ids) + 7) // 8


def table():
    return collections.defaultdict(lambda: collections.defaultdict(int))


# pick: reached while pickling outside any fixture case; pick_case: inside one.
LINE_TABLES = ("run", "loose", "imp", "pick", "pick_case", "run_body", "imp_body")
lines = {name: table() for name in LINE_TABLES}
KEY_TABLES = ("opened", "imported", "dirs", "classes", "loose_classes", "dialects",
              "loose_dialects", "wires")
wired = set()
keyed = {name: collections.defaultdict(int) for name in KEY_TABLES}
mro = {}
cases = {}   # test index -> packed blob


def add(target, per_file, bit):
    for fn, numbers in per_file.items():
        d = target[fn]
        for line in numbers:
            d[line] |= bit


records = []
for i, tid in enumerate(ids):
    for raw in open(f"{trace_dir}/trace-{tests[i]['number']}.jsonl"):
        rec = json.loads(raw)
        if rec["phase"] == "import":
            mro.update(rec.get("mro") or {})
            wired.update(rec.get("wired") or ())
        records.append((i, rec))
        break_after = False
    # keep memory flat: fold as we go
    for i2, rec in records:
        bit = 1 << i2
        if rec["phase"] == "import":
            add(lines["imp"], rec["lines"], bit)
            add(lines["imp_body"], rec["blines"], bit)
            for p in rec["files"]:
                keyed["opened"][p] |= bit
            # Folders listed before the test starts are pytest collecting, not the test.
            continue
        add(lines["run"], rec["lines"], bit)
        add(lines["loose"], rec["lines"], bit)
        add(lines["run_body"], rec["blines"], bit)
        add(lines["pick"], rec["plines"], bit)
        for name, field in (("opened", "files"), ("imported", "pyfiles"), ("dirs", "dirs"),
                            ("loose_dialects", "loose_dialects"), ("dialects", "dialects"),
                            ("classes", "classes"), ("loose_classes", "loose_classes"),
                            ("wires", "wires")):
            for key in rec.get(field) or ():
                keyed[name][key] |= bit
        if rec["cases"]:
            per_fixture = {}
            for case in rec["cases"]:
                fixture, index, case_lines, case_dialects, case_classes = case[:5]
                case_pickled = case[5] if len(case) > 5 else {}
                case_wires = case[6] if len(case) > 6 else ()
                entry = per_fixture.setdefault(
                    fixture, {"n": 0, "lines": table(), "dialects": collections.defaultdict(int),
                              "classes": collections.defaultdict(int),
                              "wires": collections.defaultdict(int)})
                entry["n"] = max(entry["n"], index + 1)
                cbit = 1 << index
                add(entry["lines"], case_lines, cbit)
                add(entry["lines"], case_pickled, cbit)
                add(lines["run"], case_lines, bit)
                add(lines["pick_case"], case_pickled, bit)
                for key in case_dialects:
                    entry["dialects"][key] |= cbit
                for key in case_classes:
                    entry["classes"][key] |= cbit
                for key in case_wires:
                    entry["wires"][key] |= cbit
            packed = {}
            for fixture, entry in per_fixture.items():
                nb = (entry["n"] + 7) // 8
                masks = {}
                table_ = []

                def cid(mask, masks=masks, table_=table_, nb=nb):
                    key = masks.get(mask)
                    if key is None:
                        key = masks[mask] = len(table_)
                        table_.append(mask.to_bytes(nb, "little"))
                    return key

                packed[fixture] = {
                    "n": entry["n"],
                    # Split by case only if some case ran code beyond the fixture loader.
                    "tagged": any(fn != "tests/helpers.py" for fn in entry["lines"]),
                    "lines": {fn: {line: cid(m) for line, m in rows.items()}
                              for fn, rows in entry["lines"].items()},
                    "dialects": {k: cid(m) for k, m in entry["dialects"].items()},
                    "classes": {k: cid(m) for k, m in entry["classes"].items()},
                    "wires": {k: cid(m) for k, m in entry["wires"].items()},
                    "sets": table_,
                }
            cases[i2] = zlib.compress(pickle.dumps(packed, protocol=5), 6)
    records.clear()

# A test that creates a class also exercises that class's bases.
for name in ("classes", "loose_classes"):
    expanded = collections.defaultdict(int)
    for key, mask in keyed[name].items():
        for base in mro.get(key, [key]):
            expanded[base] |= mask
    keyed[name] = expanded

sets = {}
set_table = []


def intern(mask):
    key = sets.get(mask)
    if key is None:
        key = sets[mask] = len(set_table)
        set_table.append(mask.to_bytes(NBYTES, "little"))
    return key


data = {
    "ids": ids,
    "cost": [costs.get(tid) for tid in ids],  # CPU seconds in the in-process runner
    "families": families,
    "mro": mro,
    "self_import": {path: frozenset(found)
                    for path, found in (self_import.get("lines") or {}).items()},
    "self_dialects": sorted(self_import.get("dialects") or ()),
    "wired": sorted(wired),
    "cases": cases,
    "sets": set_table,
}
for name in LINE_TABLES:
    data[name] = {fn: {line: intern(m) for line, m in rows.items()}
                  for fn, rows in lines[name].items()}
for name in KEY_TABLES:
    data[name] = {key: intern(m) for key, m in keyed[name].items()}
blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
open(out_file, "wb").write(blob)
print(f"{len(set_table)} distinct sets, {len(cases)} tests with fixture cases, "
      f"{len(blob) / 1e6:.2f} MB", file=sys.stderr)
