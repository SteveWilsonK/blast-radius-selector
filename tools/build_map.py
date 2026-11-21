"""Fold the per-test traces into the map the selector loads.

Usage: build_map.py tests.json trace_dir out_file [costs.json] [families.json] [self_import.json]
                    [body_trace_dir sqlglot_checkout]
The map is keyed by test id, so it does not depend on tests.json numbering.
body_trace_dir holds traces that also record each test by statement of its body.
"""
import array
import ast
import collections
import json
import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from br import slicer  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
trace_dir, out_file = sys.argv[2], sys.argv[3]
costs = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
families = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {}
self_import = json.load(open(sys.argv[6])) if len(sys.argv) > 6 else {}
body_dir = sys.argv[7] if len(sys.argv) > 8 and sys.argv[7] != "-" else None   # "-": leave them out
checkout = sys.argv[8] if len(sys.argv) > 8 else None
ids = [t["id"] for t in tests]
NBYTES = (len(ids) + 7) // 8
MIN_STATEMENTS = 4
MIN_COST = 0.002      # seconds: below this, cutting a test down saves nothing
read_ids = {}         # read tag -> small number, shared by every per-test table


def pack_reads(table):
    """{tag: set id} as two arrays of numbers: tags are long and repeat in every test."""
    pairs = sorted((read_ids.setdefault(tag, len(read_ids)), sid) for tag, sid in table.items())
    return (array.array("I", [a for a, _ in pairs]).tobytes(),
            array.array("I", [b for _, b in pairs]).tobytes())
_trees = {}
body_count = [0, 0]


def body_entry(test_id, number):
    """The per-statement tables of one test, as a pseudo-fixture "@body", or None."""
    path = f"{body_dir}/trace-{number}.jsonl"
    if not body_dir or not os.path.exists(path):
        return None
    record = None
    for raw in open(path):
        candidate = json.loads(raw)
        if candidate["phase"] == "run":
            record = candidate
    body = record and record.get("body")
    if not body or record["cases"] or body["n"] < MIN_STATEMENTS or not record.get("seconds"):
        return None
    if (costs.get(test_id) or 0.0) < MIN_COST:
        return None
    file, class_name, method = test_id.split("::")
    tree = _trees.get(file)
    if tree is None:
        tree = _trees[file] = ast.parse(open(os.path.join(checkout, file), encoding="utf-8").read())
    plan = slicer.analyse(tree, class_name, method)
    body_count[0] += 1
    # The plan must describe the very function that was traced.
    if plan is None or len(plan["spans"]) != body["n"] or plan["head"][1] + 1 != plan["spans"][0][0]:
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method \
                        and item.lineno != body["first"]:
                    return None
    nb = (body["n"] + 7) // 8
    sets = [int(text, 16).to_bytes(nb, "little") for text in body["sets"]]
    lines = {}
    for table in (body["lines"], body["plines"]):
        for fn, rows in table.items():
            target = lines.setdefault(fn, {})
            for line, sid in rows.items():
                line = int(line)
                if line in target and target[line] != sid:
                    merged = int.from_bytes(sets[target[line]], "little") | int.from_bytes(
                        sets[sid], "little")
                    sets.append(merged.to_bytes(nb, "little"))
                    sid = len(sets) - 1
                target[line] = sid
    packed_lines = {}
    for fn, rows in lines.items():
        ordered = sorted(rows)
        packed_lines[fn] = (array.array("I", ordered).tobytes(),
                            array.array("I", [rows[line] for line in ordered]).tobytes())
    body_count[1] += 1
    return {"n": body["n"], "tagged": True, "packed": packed_lines,
            "dialects": body["dialects"], "classes": body["classes"], "wires": body["wires"],
            "reads_packed": pack_reads(body.get("reads") or {}),
            "share": [round(t / record["seconds"], 5) for t in body["time"]],
            "sets": sets, "plan": plan}


def table():
    return collections.defaultdict(lambda: collections.defaultdict(int))


# pick: reached while pickling outside any fixture case; pick_case: inside one.
# load: function lines that ran while a module was loading during the test.
LINE_TABLES = ("run", "loose", "imp", "pick", "pick_case", "run_body", "imp_body", "load")
lines = {name: table() for name in LINE_TABLES}
KEY_TABLES = ("opened", "imported", "dirs", "classes", "loose_classes", "dialects",
              "loose_dialects", "wires", "reads")
tracked = set()   # class-level values whose reads were recorded
untracked = set() # ... whose recorder was later replaced: their reads are not all seen
failed_under_trace = []
shared = {}       # a second name of such a value -> the name its reads are filed under
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
            tracked.update(rec.get("tracked") or ())
            untracked.update(rec.get("untracked") or ())
            shared.update(rec.get("shared") or {})
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
        if rec.get("failed"):
            failed_under_trace.append(ids[i2])
        add(lines["run"], rec["lines"], bit)
        add(lines["loose"], rec["lines"], bit)
        add(lines["run_body"], rec["blines"], bit)
        add(lines["pick"], rec["plines"], bit)
        add(lines["load"], rec.get("llines") or {}, bit)
        for name, field in (("opened", "files"), ("imported", "pyfiles"), ("dirs", "dirs"),
                            ("loose_dialects", "loose_dialects"), ("dialects", "dialects"),
                            ("classes", "classes"), ("loose_classes", "loose_classes"),
                            ("wires", "wires"), ("reads", "reads")):
            for key in rec.get(field) or ():
                keyed[name][key] |= bit
        if rec["cases"]:
            per_fixture = {}
            for case in rec["cases"]:
                fixture, index, case_lines, case_dialects, case_classes = case[:5]
                case_pickled = case[5] if len(case) > 5 else {}
                case_wires = case[6] if len(case) > 6 else ()
                case_time = case[7] if len(case) > 7 else 0.0
                case_reads = case[8] if len(case) > 8 else ()
                entry = per_fixture.setdefault(
                    fixture, {"n": 0, "lines": table(), "dialects": collections.defaultdict(int),
                              "classes": collections.defaultdict(int),
                              "wires": collections.defaultdict(int),
                              "reads": collections.defaultdict(int), "time": {}})
                entry["n"] = max(entry["n"], index + 1)
                entry["time"][index] = entry["time"].get(index, 0.0) + case_time
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
                for key in case_reads:
                    entry["reads"][key] |= cbit
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
                    "reads_packed": pack_reads({k: cid(m) for k, m in entry["reads"].items()}),
                    # Share of the whole test's time spent in each case, when timed.
                    "share": ([round(entry["time"].get(i, 0.0) / rec["seconds"], 5)
                               for i in range(entry["n"])]
                              if rec.get("seconds") else None),
                    "sets": table_,
                }
            cases[i2] = zlib.compress(pickle.dumps(packed, protocol=5), 6)
        elif body_dir:
            entry = body_entry(tid, tests[i]["number"])
            if entry is not None:
                cases[i2] = zlib.compress(pickle.dumps({"@body": entry}, protocol=5), 6)
    records.clear()

# Functions that fill a process-wide cache the first time something is used: like
# code that runs while a module loads, what they do stays for the rest of the test.
ONCE = (("sqlglot/generator.py", "_build_dispatch"),)
if checkout:
    for once_path, once_name in ONCE:
        once_tree = ast.parse(open(os.path.join(checkout, once_path), encoding="utf-8").read())
        spans = [(n.lineno, n.end_lineno) for n in ast.walk(once_tree)
                 if isinstance(n, ast.FunctionDef) and n.name == once_name]
        assert spans, f"{once_name} not found in {once_path}"
        for first, last in spans:
            for line in range(first, last + 1):
                ran = lines["run"].get(once_path, {}).get(line)
                if ran:
                    lines["load"][once_path][line] |= ran

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
    "tracked": sorted(tracked - untracked),
    "read_ids": read_ids,
    "shared": shared,
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
if failed_under_trace:
    print(f"FAILED WHILE TRACED (their coverage is cut short): {failed_under_trace}", file=sys.stderr)
print(f"{len(tracked - untracked)} recorded values ({len(untracked)} dropped), ", end="", file=sys.stderr)
print(f"{len(set_table)} distinct sets, {len(cases)} tests with cases "
      f"({body_count[1]} of {body_count[0]} by statement), {len(blob) / 1e6:.2f} MB", file=sys.stderr)
