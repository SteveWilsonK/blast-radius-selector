"""Load the coverage map and translate it to this run's test numbers."""
import pickle
import zlib

from . import blob


class _Reads:
    """Per-test table of recorded reads: tag -> key of the set of cases reading it."""

    def __init__(self, packed, ids):
        import array

        a, b = array.array("I"), array.array("I")
        a.frombytes(packed[0])
        b.frombytes(packed[1])
        self._table = dict(zip(a, b))
        self._ids = ids

    def get(self, tag, default=None):
        number = self._ids.get(tag)
        return default if number is None else self._table.get(number, default)

    def __bool__(self):
        return True


class Maps:
    def __init__(self, path, tests):
        data = blob.load(path)
        self.ids = data["ids"]
        for name in ("run", "loose", "imp", "pick", "pick_case", "run_body", "imp_body", "load", "opened",
                     "imported", "dirs", "classes", "loose_classes", "dialects",
                     "loose_dialects", "families", "mro", "wires", "self_import", "reads"):
            setattr(self, name, data.get(name) or {})
        # Without the "load" table the map cannot tell which functions ran while a
        # module loaded, so nothing may be narrowed below the whole test on line evidence.
        self.has_load = "load" in data
        self.wired = frozenset(data.get("wired") or ())
        self.tracked = frozenset(data.get("tracked") or ())   # values whose reads are recorded
        self.shared = data.get("shared") or {}
        self.read_ids = data.get("read_ids") or {}
        self.self_dialects = frozenset(data.get("self_dialects") or ())
        self._cases = data.get("cases") or {}
        self._case_cache = {}
        self._sets = data["sets"]
        self._masks = {}
        # Reads of class-level values may come from their own, newer file.
        self.reads_version = 2
        import os

        extra = os.path.join(os.path.dirname(path), "reads.bin")
        if os.path.exists(extra):
            more = blob.load(extra)
            if more.get("ids") == self.ids:
                base = len(self._sets)
                self._sets = list(self._sets) + list(more["sets"])
                self.reads = {tag: base + key for tag, key in more["reads"].items()}
                self.tracked = frozenset(more["tracked"])
                self.shared = more["shared"]
                self.reads_version = more.get("version", 2)
        number_of = {t["id"]: t["number"] for t in tests}
        # bit position in the map -> test number in this run (None when the test is gone)
        self.number = [number_of.get(tid) for tid in self.ids]
        self.cost = {n: c for n, c in zip(self.number, data.get("cost") or []) if n is not None
                     and c is not None}
        self.unknown = sorted(set(number_of) - set(self.ids))  # tests the map never saw

    def cases(self, bit_index):
        """Fixture cases of one test: fixture -> {n, lines, dialects, classes, sets}."""
        entry = self._case_cache.get(bit_index, False)
        if entry is False:
            blob = self._cases.get(bit_index)
            entry = pickle.loads(zlib.decompress(blob)) if blob is not None else None
            for data in (entry or {}).values():
                packed = data.pop("reads_packed", None)
                if packed is not None:
                    data["reads"] = _Reads(packed, self.read_ids)
            body = entry.get("@body") if entry else None
            if body is not None and "lines" not in body:
                import array

                lines = {}
                for path, (numbers, keys) in body["packed"].items():
                    a, b = array.array("I"), array.array("I")
                    a.frombytes(numbers)
                    b.frombytes(keys)
                    lines[path] = dict(zip(a, b))
                body["lines"] = lines
            self._case_cache[bit_index] = entry
        return entry

    def has_cases(self, bit_index):
        return bit_index in self._cases

    def mask(self, key):
        m = self._masks.get(key)
        if m is None:
            m = self._masks[key] = int.from_bytes(self._sets[key], "little")
        return m

    def line(self, table, path, line):
        key = table.get(path, {}).get(line)
        return 0 if key is None else self.mask(key)

    def lines(self, table, path, first, last):
        rows = table.get(path)
        if not rows:
            return 0
        m = 0
        for line in range(first, last + 1):
            key = rows.get(line)
            if key is not None:
                m |= self.mask(key)
        return m

    def file_any(self, table, path):
        m = 0
        for key in set(table.get(path, {}).values()):
            m |= self.mask(key)
        return m

    def bits(self, mask):
        """(map position, test number) for every test in the mask that this run has."""
        out = []
        number = self.number
        while mask:
            low = mask & -mask
            i = low.bit_length() - 1
            n = number[i]
            if n is not None:
                out.append((i, n))
            mask ^= low
        return out

    def case_total(self, bit_index):
        entry = self.cases(bit_index)
        return sum(data["n"] for data in entry.values()) if entry else 0

    def numbers(self, mask):
        out = []
        number = self.number
        i = 0
        while mask:
            low = mask & -mask
            i = low.bit_length() - 1
            n = number[i]
            if n is not None:
                out.append(n)
            mask ^= low
        return out
