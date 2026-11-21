"""From a patch to the tests that could notice it, using maps of the pinned commit."""
import ast
import os
import pickle
import zlib

from . import patch as patchlib
from . import units


class Analysis:
    """What one commit touches.

    strong:   tests that could notice the change (bitmask over map positions).
    whole:    of those, tests that must run every fixture case they load. The others
              still run all their own code, but of the fixture files they walk only
              the cases named by `specs`.
    specs:    how to find the fixture cases of a test that meet the change.
    """

    def __init__(self):
        self.strong = 0
        self.whole = 0
        self.specs = []
        self.everything = False
        self.pickle = False       # a touched unit is reached while pickling for pool workers
        self.py_changed = []      # (path, new_text or None)
        self.data_changed = []    # (path, new_text or None)
        self.notes = []
        self.units = []           # (path, kind, first, last)
        self.changed_fixtures = {}  # fixture name -> tests that read the patched file

    @property
    def data_only(self):
        """Every effective change is in a data file some test reads."""
        return bool(self.data_changed) and not self.units and not self.everything and all(
            note.startswith(("read:", "noop:")) for note in self.notes)


FIXTURES = "tests/fixtures/"


def module_of(path):
    name = path[:-3].replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


class Analyzer:
    def __init__(self, maps, source_root, refs_path=None):
        self.maps = maps
        self.root = source_root
        self._text = {}
        self._tree = {}
        self._refs_path = refs_path
        self._refs = None
        self._subclasses = {}

    def refs(self, name):
        if self._refs is None:
            self._refs = {}
            if self._refs_path and os.path.exists(self._refs_path):
                with open(self._refs_path, "rb") as f:
                    self._refs = pickle.loads(zlib.decompress(f.read()))
        return self._refs.get(name)

    def text(self, path):
        if path not in self._text:
            try:
                with open(os.path.join(self.root, path), encoding="utf-8") as f:
                    self._text[path] = f.read()
            except (OSError, UnicodeDecodeError):
                self._text[path] = None
        return self._text[path]

    def tree(self, path):
        if path not in self._tree:
            text = self.text(path)
            try:
                self._tree[path] = ast.parse(text) if text is not None else None
            except SyntaxError:
                self._tree[path] = None
        return self._tree[path]

    def subclasses(self, key):
        """Class keys whose base-class chain contains `key` (itself included)."""
        found = self._subclasses.get(key)
        if found is None:
            found = self._subclasses[key] = [k for k, bases in self.maps.mro.items() if key in bases]
            if key not in found:
                found.append(key)
        return found

    # ---- whole patch -----------------------------------------------------

    def analyze(self, patch_text):
        maps = self.maps
        a = Analysis()
        for fp in patchlib.parse(patch_text):
            paths = [p for p in {fp.old_path, fp.new_path} if p]
            for path in paths:
                key = maps.opened.get(path)
                if key is not None:
                    readers = maps.mask(key)
                    a.strong |= readers
                    if path.startswith(FIXTURES) and fp.old_path == fp.new_path:
                        # Tests that walk it case by case only need the cases that changed.
                        name = path[len(FIXTURES):]
                        a.changed_fixtures[name] = a.changed_fixtures.get(name, 0) | readers
                    else:
                        a.whole |= readers
                    a.notes.append(f"read:{path}")
            if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
                # Files appearing, disappearing or moving are seen by tests that list
                # their folder, whatever the file holds.
                for path in paths:
                    folder = os.path.dirname(path) + "/"
                    while True:
                        key = maps.dirs.get(folder)
                        if key is not None:
                            listers = maps.mask(key)
                            a.strong |= listers
                            a.whole |= listers
                            a.notes.append(f"listed:{folder}")
                        if folder in ("/", ""):
                            break
                        folder = os.path.dirname(folder.rstrip("/"))
                        folder = folder + "/" if folder else ""
            old_path = fp.old_path
            if old_path is None:
                a.notes.append(f"new:{fp.new_path}")
                if fp.new_path and fp.new_path.endswith(".py"):
                    a.py_changed.append((fp.new_path, None))
                continue
            if not old_path.endswith(".py"):
                old_text = self.text(old_path)
                new_text = None
                if old_text is not None and fp.new_path and not fp.binary:
                    new_text = patchlib.apply(old_text, fp)
                a.data_changed.append((old_path, new_text))
                continue
            self._python(a, fp)
        return a

    def _loaders(self, path):
        """Every test that loads this source file at all."""
        maps = self.maps
        mask = 0
        for table in (maps.run, maps.imp, maps.pick, maps.pick_case, maps.run_body,
                      maps.imp_body):
            mask |= maps.file_any(table, path)
        return mask

    def _python(self, a, fp):
        maps = self.maps
        path = fp.old_path
        tables = (maps.run, maps.imp, maps.pick, maps.pick_case, maps.run_body, maps.imp_body)
        known = any(path in table for table in tables)
        old_text = self.text(path)
        moved = fp.new_path != path
        new_text = None
        if old_text is not None and fp.new_path and not fp.binary:
            new_text = patchlib.apply(old_text, fp)
        a.py_changed.append((path, new_text))
        if not known:
            a.notes.append(f"unmapped:{path}")
            return
        if moved or new_text is None:
            # Deleted, renamed, or a patch that does not fit: importing may break.
            loaders = self._loaders(path)
            a.strong |= loaders
            a.whole |= loaders
            a.notes.append(f"whole:{path}")
            return
        tree = self.tree(path)
        try:
            new_tree = ast.parse(new_text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            loaders = self._loaders(path)
            a.strong |= loaders
            a.whole |= loaders
            a.notes.append(f"syntax:{path}")
            return
        if tree is None:
            loaders = self._loaders(path)
            a.strong |= loaders
            a.whole |= loaders
            return
        removed, added = patchlib.line_changes(fp)
        seen = set()
        before = len(a.units)
        # Old statements that lost or changed lines.
        for old_line, new_position in removed:
            kind, first, last, classes, names = units.locate(tree, old_line)
            if kind is None or (first, last) in seen:
                continue
            seen.add((first, last))
            old_statement = units.statement_at(tree, old_line)
            new_first = new_position - (old_line - first)
            twin = units.statement_at(new_tree, new_first) if new_first > 0 else None
            if kind != "func" or units.enclosing_function(tree, old_line) is None or True:
                if twin is not None and units.signature(twin) == units.signature(old_statement):
                    continue  # same statement, differently laid out or commented
            self._unit(a, path, kind, first, last, classes, names)
        # New lines: either part of a statement handled above, or a statement of their own.
        for new_line, old_position in added:
            statement = units.statement_at(new_tree, new_line)
            if statement is None:
                continue  # blank or comment
            kind, first, last, classes, names = units.locate(tree, old_position)
            old_statement = units.statement_at(tree, old_position) if kind else None
            twin_old = None
            for probe in (old_position, old_position - 1):
                candidate = units.statement_at(tree, probe) if probe > 0 else None
                if candidate is not None and units.signature(candidate) == units.signature(statement):
                    twin_old = candidate
                    break
            if twin_old is not None:
                continue  # the line belongs to a statement that did not change
            touched_old = False
            for probe in (old_position, old_position - 1):
                k, f, l, c, nm = units.locate(tree, probe) if probe > 0 else (None, 0, 0, [], [])
                if k is not None and (f, l) in seen:
                    touched_old = True
            if touched_old:
                continue  # already counted through the old statement it rewrites
            # A statement that was not there before. It runs whenever its block does:
            # inside a function, take every test that runs any line of that function.
            span = units.enclosing_function(tree, old_position) or (
                units.enclosing_function(tree, old_position - 1) if old_position > 1 else None)
            if span is not None:
                if span not in seen:
                    seen.add(span)
                    self._unit(a, path, "func", span[0], span[1], [], [])
            else:
                new_kind, _, _, new_classes, new_names = units.locate(new_tree, new_line)
                if new_kind == "doc":
                    continue
                anchor = None
                for probe in (old_position, old_position - 1, old_position + 1):
                    k, f, l, c, nm = units.locate(tree, probe) if probe > 0 else (None, 0, 0, [], [])
                    if k is not None:
                        anchor = (f, l)
                        break
                if anchor is None or anchor in seen and not new_names:
                    if anchor is None:
                        loaders = self._loaders(path)
                        a.strong |= loaders
                        a.whole |= loaders
                        a.notes.append(f"module:{path}")
                    continue
                seen.add(anchor)
                self._unit(a, path, "import", anchor[0], anchor[1], new_classes, new_names)
        if len(a.units) == before:
            a.notes.append(f"noop:{path}")

    def _unit(self, a, path, kind, first, last, classes, names):
        if kind == "doc":
            a.notes.append(f"docstring:{path}:{first}")
            return
        a.units.append((path, kind, first, last))
        if kind == "func":
            self._function_unit(a, path, first, last)
        else:
            self._body_unit(a, path, first, last, classes, names)

    # ---- one unit ---------------------------------------------------------

    def _function_unit(self, a, path, first, last):
        """Code that runs each time its function or lambda is called."""
        maps = self.maps
        tests = maps.lines(maps.run, path, first, last)
        via_pickle = maps.lines(maps.pick, path, first, last)
        in_case_pickle = maps.lines(maps.pick_case, path, first, last)
        at_import = maps.lines(maps.imp, path, first, last)
        if via_pickle or in_case_pickle:
            a.pickle = True
        a.strong |= tests | via_pickle | in_case_pickle | at_import
        # A function that also runs while modules load (metaclasses, decorators, table
        # builders) shapes everything loaded after it: those tests run in full.
        a.whole |= via_pickle | at_import
        a.specs.append(("lines", [(path, first, last)], None, 0))
        a.notes.append(f"func:{path}:{first}-{last}" + ("+import" if at_import else ""))

    def _body_unit(self, a, path, first, last, classes, names):
        """A class- or module-level statement: it runs once, when its module loads."""
        maps = self.maps
        lazy = maps.lines(maps.run_body, path, first, last)      # loaded during the test
        eager = maps.lines(maps.imp_body, path, first, last)     # loaded before it
        loaders = lazy | eager
        if not loaders:
            a.notes.append(f"unloaded:{path}:{first}")
            return
        class_key = module_of(path) + ":" + ".".join(classes) if classes else None
        if class_key is not None and class_key in maps.classes:
            # An expression class: the tests that create it or a subclass of it.
            tests = maps.mask(maps.classes[class_key]) & loaders
            a.strong |= tests
            a.specs.append(("classes", self.subclasses(class_key), None, 0))
            a.notes.append(f"class:{class_key}")
            return
        family = maps.families.get(path)
        users = loose_users = 0
        if family:
            for key in family:
                k = maps.dialects.get(key)
                if k is not None:
                    users |= maps.mask(k)
                k = maps.loose_dialects.get(key)
                if k is not None:
                    loose_users |= maps.mask(k)
        # Where the bound names are read. A read in a function that only runs while
        # tests execute pins the change to the tests that execute it. A read at import
        # time means the value feeds other tables, so it cannot be pinned this way.
        readers = None
        places = []
        if names:
            readers = 0
            for name in names:
                found = self.refs(name)
                if not found:
                    readers = None
                    break
                for ref_path, ref_kind, ref_first, ref_last in found:
                    if ref_kind != "func" or maps.lines(maps.imp, ref_path, ref_first, ref_last):
                        readers = None
                        break
                    places.append((ref_path, ref_first, ref_last))
                if readers is None:
                    break
            if readers is not None:
                loose = 0
                for ref_path, ref_first, ref_last in places:
                    readers |= maps.lines(maps.run, ref_path, ref_first, ref_last)
                    picked = maps.lines(maps.pick, ref_path, ref_first, ref_last)
                    in_case = maps.lines(maps.pick_case, ref_path, ref_first, ref_last)
                    if picked or in_case:
                        a.pickle = True
                    readers |= picked | in_case
                    loose |= picked
        if readers is not None:
            tests = readers & loaders
            if family:
                tests &= users
            a.strong |= tests
            a.whole |= loose & tests
            a.specs.append(("lines", places, family, loose_users))
            a.notes.append(f"reads:{path}:{first}:{'/'.join(names)}")
        elif family:
            tests = (users | lazy) & loaders
            a.strong |= tests
            a.whole |= (lazy & ~users) & tests
            a.specs.append(("dialects", family, None, 0))
            a.notes.append(f"family:{path}:{first}:{len(family)}")
        else:
            a.strong |= loaders
            a.whole |= loaders
            a.notes.append(f"module:{path}:{first}")

    # ---- fixture cases ----------------------------------------------------

    def needs(self, a, bit_index):
        """Fixture cases one test must run: None for no restriction, else
        {fixture: set of case indexes, or None for all of that fixture}."""
        if (a.whole >> bit_index) & 1:
            return None
        entry = self.maps.cases(bit_index)
        if entry is None:
            return None
        for fixture, readers in a.changed_fixtures.items():
            if (readers >> bit_index) & 1 and fixture not in entry:
                return None  # reads the patched file some other way than case by case
        result = {}
        for fixture, data in entry.items():
            if (a.changed_fixtures.get(fixture, 0) >> bit_index) & 1:
                result[fixture] = "changed"
                continue
            if not data.get("tagged"):
                # The test walks this fixture in a way the map could not split by case.
                # With no code change, cases of an untouched fixture cannot differ.
                result[fixture] = None if a.specs else set()
                continue
            sets = data["sets"]
            mask = 0
            for kind, what, family, loose_users in a.specs:
                found = 0
                if kind == "lines":
                    for path, first, last in what:
                        rows = data["lines"].get(path)
                        if rows:
                            for line in range(first, last + 1):
                                cid = rows.get(line)
                                if cid is not None:
                                    found |= int.from_bytes(sets[cid], "little")
                    if family and found and not (loose_users >> bit_index) & 1:
                        used = 0
                        for key in family:
                            cid = data["dialects"].get(key)
                            if cid is not None:
                                used |= int.from_bytes(sets[cid], "little")
                        found &= used
                else:
                    table = data["dialects"] if kind == "dialects" else data["classes"]
                    for key in what:
                        cid = table.get(key)
                        if cid is not None:
                            found |= int.from_bytes(sets[cid], "little")
                mask |= found
            indexes = set()
            m = mask
            while m:
                low = m & -m
                indexes.add(low.bit_length() - 1)
                m ^= low
            result[fixture] = indexes
        return result
