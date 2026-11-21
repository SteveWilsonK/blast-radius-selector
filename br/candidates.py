"""From a patch to the tests that could notice it, using maps of the pinned commit."""
import ast
import os
import pickle
import zlib

from . import patch as patchlib
from . import relations
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
        self.direct = 0           # tests that read a patched file: affected for certain
        self.loose = 0            # tests whose own code (outside fixture cases) meets the change
        self.whole = 0
        self.specs = []
        self.everything = False
        self.pickle = False       # a touched unit is reached while pickling for pool workers
        self.py_changed = []      # (path, new_text or None)
        self.data_changed = []    # (path, new_text or None)
        self.notes = []
        self.units = []           # (path, kind, first, last)
        self.changed_fixtures = {}  # fixture name -> tests that read the patched file
        self.body_units = 0       # class- or module-level changes in sqlglot
        self.deferred = 0         # ... of which left to the state comparison

    @property
    def data_only(self):
        """Every effective change is in a data file some test reads."""
        return bool(self.data_changed) and not self.units and not self.everything and all(
            note.startswith(("read:", "noop:")) for note in self.notes)


FIXTURES = "tests/fixtures/"
# An Enum's own tables, which change with any member.
ENUM_INNARDS = ("_member_map_", "_value2member_map_", "_hashable_values_", "_member_names_",
                "_unhashable_values_", "_value_repr_")
BASE_DIALECT = "sqlglot.dialects.dialect:Dialect"


def module_of(path):
    name = path[:-3].replace("/", ".")
    return name[:-9] if name.endswith(".__init__") else name


class Analyzer:
    def __init__(self, maps, source_root, refs_path=None, cond_path=None):
        self._cond_path = cond_path
        self._cond = False
        self.maps = maps
        self.root = source_root
        self._text = {}
        self._tree = {}
        self._state = None
        self._refs_path = refs_path
        self._refs = None
        self._enumdyn = None
        self._subclasses = {}

    def refs(self, name):
        if self._refs is None:
            self._refs = {}
            if self._refs_path and os.path.exists(self._refs_path):
                with open(self._refs_path, "rb") as f:
                    self._refs = pickle.loads(zlib.decompress(f.read()))
        return self._refs.get(name)

    def _enum_dynamic(self):
        """Enum class key -> code units reaching its members without naming them
        (tools/build_enumdyn.py, built with the image)."""
        if self._enumdyn is None:
            self._enumdyn = {}
            path = os.path.join(os.path.dirname(self._refs_path or ""), "enumdyn.bin")
            if self._refs_path and os.path.exists(path):
                with open(path, "rb") as f:
                    self._enumdyn = pickle.loads(zlib.decompress(f.read()))["dynamic"]
        return self._enumdyn

    def cond(self):
        """Comparison coverage, or None when it is absent or does not match this run."""
        if self._cond is False:
            self._cond = None
            if self._cond_path and os.path.exists(self._cond_path):
                with open(self._cond_path, "rb") as f:
                    data = pickle.loads(zlib.decompress(f.read()))
                if data.get("complete") and data.get("ids") == self.maps.ids:
                    self._cond = data
        return self._cond

    def _swap_reach(self, path, swap):
        """Tests in which swapping one comparison's operator could change its result:
        those that saw the operands relate in a way the two operators answer
        differently. None when comparison coverage cannot settle it."""
        cond = self.cond()
        line, column, old_op, new_op = swap
        if cond is None or not path.startswith("sqlglot/") or path in cond["skipped"]:
            return None
        # A comparison sqlglot also evaluates while it loads is fine too: each test was
        # recorded in a process of its own from import onwards, so the evaluations made
        # while loading are in every record that loaded it, and a swap that changes one
        # of them reaches every such test.
        mask = 0
        for code, key in cond["sites"].get(f"{path}:{line}:{column}", {}).items():
            if relations.differs(old_op, new_op, code):
                mask |= int.from_bytes(cond["sets"][key], "little")
        return mask

    def _bool_reach(self, path, swap):
        """Tests in which swapping `and` for `or` (or back) could change what one
        boolean expression decides: those that saw a truth pair the two operators
        answer differently. None when the recorded truths cannot settle it."""
        cond = self.cond()
        line, column, old_op, _ = swap
        if cond is None or not path.startswith("sqlglot/") or path in cond["skipped"]:
            return None
        if not cond.get("bools"):
            return None   # recorded before and/or were followed
        codes = cond["sites"].get(f"bool:{path}:{line}:{column}")
        if codes is None:
            return None   # never seen evaluated: nothing to go on
        mask = 0
        for code, key in codes.items():
            if code in relations.BOOL_DIFFERS[old_op]:
                mask |= int.from_bytes(cond["sets"][key], "little")
        return mask

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

    def _at_load(self, path, first, last):
        """Does sqlglot itself run any of these lines while its modules load?"""
        found = self.maps.self_import.get(path)
        if found is None:
            return False
        return any(line in found for line in range(first, last + 1))

    def subclasses(self, key):
        """Class keys whose base-class chain contains `key` (itself included)."""
        found = self._subclasses.get(key)
        if found is None:
            found = self._subclasses[key] = [k for k, bases in self.maps.mro.items() if key in bases]
            if key not in found:
                found.append(key)
        return found

    # ---- whole patch -----------------------------------------------------

    def state_tags(self, result, changed_modules=()):
        """Turn a state comparison (see state.py) into the recorded reads that can
        notice it: a list of read tags, or None when some difference is of a kind
        the map does not follow and the older rules must decide."""
        maps = self.maps
        if not result or result.get("error") or result.get("truncated") or not maps.tracked:
            return None
        tags = set()

        from .state import miss_group_of_name

        grouped = maps.reads_version >= 3   # missing-key lookups filed by group of keys

        def entry(key, marks):
            """A class-level value or one entry of a class-level dict."""
            if "\t" in key or "|" not in key:
                return False      # a module-level name: reads of those are not recorded
            parts = key.split("|")
            holder = parts[0] + "|" + parts[1]
            holder = maps.shared.get(holder, holder)
            if holder not in maps.tracked:
                return False
            if len(parts) == 2:
                tags.add(holder)
            else:
                name = "|".join(parts[2:])
                for mark in marks:
                    if mark == "key":
                        tags.add(holder + "|" + name)
                    elif mark == "!" and grouped:
                        # Tests that looked for a key of this one's group and found none,
                        # and those whose missing key had no stable name.
                        tags.add(holder + "|!" + miss_group_of_name(name))
                        tags.add(holder + "|!")
                    else:
                        tags.add(holder + "|" + mark)
            return True

        def by_reads(key):
            """A value the recorded reads do not follow (a module-level name, an Enum
            member, a value on a class the tracer leaves alone): the tests that run
            code reading its name, plus, for an Enum member, code that reaches members
            without naming them. Reads made while sqlglot loads need nothing here:
            whatever they build is part of the comparison and is judged on its own."""
            if "\t" in key:
                name = key.split("\t", 1)[1]
                holder = None
            elif "|" in key:
                parts = key.split("|")
                holder, name = parts[0], parts[1]
                if name.startswith("_") and name.endswith("_"):
                    if holder in self._enum_dynamic() and name in ENUM_INNARDS:
                        # An Enum's own lookup tables follow its members: code that
                        # uses them reaches members without naming them, listed below.
                        add_dynamic(holder)
                        return True
                    return False   # other machinery: not followed by name
            else:
                return False
            places = self.refs(name)
            if not places:
                return False
            for ref_path, ref_kind, ref_first, ref_last in places:
                if ref_kind == "merge":
                    continue
                if ref_kind == "import":
                    if not ref_path.startswith("sqlglot/"):
                        return False   # a test module reads it while loading: not compared
                    continue
                lines.add((ref_path, ref_first, ref_last))
            if holder is not None and holder in self._enum_dynamic():
                if not add_dynamic(holder):
                    return False
            return True

        def add_dynamic(holder):
            for ref_path, ref_kind, ref_first, ref_last in self._enum_dynamic()[holder]:
                if ref_kind == "import":
                    if not ref_path.startswith("sqlglot/"):
                        unpinned.append(holder)
                        return False
                    continue
                lines.add((ref_path, ref_first, ref_last))
            return True

        lines = set()
        unpinned = []
        for key in result.get("changed") or ():
            if key.endswith("\tcode"):
                base = key[:-5]
                if base.count("|") >= 2:
                    # A function stored as a dict entry: whoever fetches the entry.
                    if not entry(base, ("key", "*")) and not by_reads(base):
                        return None
                # Otherwise a function whose own source changed: line coverage has it.
                continue
            if not entry(key, ("key", "*")) and not by_reads(key):
                return None
        for key in result.get("removed") or ():
            if key.endswith("\tcode"):
                key = key[:-5]
            if not (key.count("|") >= 2 and entry(key, ("key", "#", "*"))) and not by_reads(key):
                return None
        for key in result.get("added") or ():
            if key.endswith("\tcode"):
                key = key[:-5]
            if not (key.count("|") >= 2 and entry(key, ("key", "!", "#", "*"))) and not by_reads(key):
                return None
        if unpinned:
            return None
        for path, first, last in sorted(lines):
            tags.add(f"@lines|{path}|{first}|{last}")
        if not tags:
            return None       # nothing visible changed: do not conclude from silence
        return sorted(tags)

    def _apply_state(self, a, tags):
        maps = self.maps
        tests = 0
        places = []
        for tag in tags:
            if tag.startswith("@lines|"):
                _, path, first, last = tag.split("|")
                first, last = int(first), int(last)
                places.append((path, first, last))
                picked = maps.lines(maps.pick, path, first, last) | \
                    maps.lines(maps.pick_case, path, first, last)
                if picked:
                    a.pickle = True
                tests |= maps.lines(maps.run, path, first, last) | picked
                a.loose |= maps.lines(maps.loose, path, first, last)
                continue
            key = maps.reads.get(tag)
            if key is not None:
                tests |= maps.mask(key)
        a.strong |= tests
        a.loose |= tests      # the map does not say where outside a case it is read
        a.specs.append(("reads", [t for t in tags if not t.startswith("@lines|")], None, 0))
        if places:
            a.specs.append(("lines", places, None, 0))
        a.notes.append(f"state:{len(tags)} names")

    def analyze(self, patch_text, state=None):
        """state: read tags from state_tags(), when the patched code was loaded and
        compared with the pinned commit. Class- and module-level changes in sqlglot
        are then judged by who reads the values that came out different."""
        maps = self.maps
        a = Analysis()
        self._state = state
        for fp in patchlib.parse(patch_text):
            paths = [p for p in {fp.old_path, fp.new_path} if p]
            if any(os.path.basename(p) in ("conftest.py", "pytest.ini", "tox.ini") for p in paths):
                # pytest itself reads these; the in-process runner cannot stand in for it.
                a.everything = True
                a.notes.append(f"pytest-config:{paths[0]}")
                continue
            for path in paths:
                key = maps.opened.get(path)
                if key is not None:
                    readers = maps.mask(key)
                    a.strong |= readers
                    a.direct |= readers
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
                            a.direct |= listers
                            a.whole |= listers
                            a.notes.append(f"listed:{folder}")
                        if folder in ("/", ""):
                            break
                        folder = os.path.dirname(folder.rstrip("/"))
                        folder = folder + "/" if folder else ""
            old_path = fp.old_path
            if old_path is None:
                a.notes.append(f"new:{fp.new_path}")
                # A file that was not there cannot be shown to folder listings from
                # memory, so a commit that adds one is never verified by running.
                if fp.new_path and fp.new_path.endswith(".py"):
                    a.py_changed.append((fp.new_path, None))
                elif fp.new_path:
                    a.data_changed.append((fp.new_path, None))
                continue
            if not old_path.endswith(".py"):
                old_text = self.text(old_path)
                new_text = None
                if old_text is not None and fp.new_path == old_path and not fp.binary:
                    new_text = patchlib.apply(old_text, fp)
                a.data_changed.append((old_path, new_text))
                continue
            self._python(a, fp)
        if state is not None and a.deferred:
            self._apply_state(a, state)
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
        if old_text is not None and fp.new_path and not fp.binary and not moved:
            new_text = patchlib.apply(old_text, fp)
        # A moved or removed file cannot be stood in for from memory: its text stays
        # None, which keeps the commit out of verification.
        a.py_changed.append((path, new_text))
        if not known:
            if os.path.basename(path) == "__init__.py":
                # An empty package file gaining code: it runs for every test that
                # loads anything from the package.
                prefix = os.path.dirname(path) + "/"
                loaders = 0
                for table in tables:
                    for other in table:
                        if other.startswith(prefix):
                            loaders |= maps.file_any(table, other)
                a.strong |= loaders
                a.whole |= loaders
                a.notes.append(f"package:{path}")
                return
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
            # A byte-order mark is legal at the top of a source file.
            new_tree = ast.parse(new_text[1:] if new_text.startswith("\ufeff") else new_text)
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
        tables_done = self._tables_done = set()
        before = len(a.units)
        # Old statements that lost or changed lines.
        for old_line, new_position in removed:
            kind, first, last, classes, names = units.locate(tree, old_line)
            if kind is None or (first, last) in seen:
                continue
            old_statement = units.statement_at(tree, old_line)
            table = units.table_statement(old_statement) if kind == "import" and classes else None
            entry = units.table_entry(table[1], old_line) if table else None
            if entry is not None and entry[0] is not None:
                mark = (first, last, entry[0])
                if mark in seen:
                    continue
                seen.add(mark)
            new_first = new_position - (old_line - first)
            twin = units.statement_at(new_tree, new_first) if new_first > 0 else None
            if twin is not None and units.signature(twin) == units.signature(old_statement):
                seen.add((first, last))
                continue  # same statement, differently laid out or commented
            if entry is not None and entry[0] is not None and self._table_entry(
                    a, path, classes, table, entry, units.table_statement(twin)):
                continue
            seen.add((first, last))
            reach = None
            bswap = None
            if kind == "func":
                swap = relations.operator_swap(old_statement, twin)
                if swap is not None:
                    reach = self._swap_reach(path, swap)
                else:
                    bswap = relations.bool_swap(old_statement, twin)
                    if bswap is not None:
                        reach = self._bool_reach(path, bswap)
            before = (a.strong, a.whole, a.loose)
            self._unit(a, path, kind, first, last, classes, names)
            if reach is not None:
                # Only one comparison's operator changed. A test in which every
                # evaluation of it gives the same answer under both operators runs
                # exactly as before, so only the others stay candidates.
                a.strong = before[0] | ((a.strong & ~before[0]) & reach)
                a.whole = before[1] | ((a.whole & ~before[1]) & reach)
                a.loose = before[2] | ((a.loose & ~before[2]) & reach)
                if bswap is not None:
                    a.notes.append(f"boolswap:{path}:{bswap[0]}:{bswap[2].__name__}>{bswap[3].__name__}")
                else:
                    a.notes.append(f"swap:{path}:{swap[0]}:{swap[2].__name__}>{swap[3].__name__}")
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
                if k is not None and ((f, l) in seen or (f, l) in tables_done):
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

    def _table_entry(self, a, path, classes, table, entry, new_table):
        """One entry of a class-level table changed. Returns False when the change
        cannot be pinned to that entry and the whole statement must be handled."""
        maps = self.maps
        name, dict_node = table
        key, is_lambda, first, last = entry
        if new_table is None or new_table[0] != name:
            return False
        if not is_lambda and path.startswith("sqlglot/"):
            a.body_units += 1
            if self._state is not None:
                statement = units.statement_at(self.tree(path), dict_node.lineno)
                stmt_first = statement.lineno if statement is not None else dict_node.lineno
                self._tables_done.add((stmt_first, dict_node.end_lineno))
                a.deferred += 1
                a.units.append((path, "state", first, last))
                return True
        old_keys = units.table_keys(dict_node)
        new_keys = units.table_keys(new_table[1])
        if set(k for k in new_keys if k is not None) - set(k for k in old_keys if k is not None):
            return False  # a key that was not there before: any test may now find it
        if new_keys.count(None) != old_keys.count(None):
            return False  # the tables this one is built from changed
        statement = units.statement_at(self.tree(path), dict_node.lineno)
        stmt_first = statement.lineno if statement is not None else dict_node.lineno
        self._tables_done.add((stmt_first, dict_node.end_lineno))
        tag = f"{module_of(path)}:{'.'.join(classes)}|{name}|{key}"
        if is_lambda:
            # The entry is the lambda itself: the tests that call it, by its lines
            # and by the record of calls through the table.
            a.units.append((path, "func", first, last))
            self._function_unit(a, path, first, last)
            callers = maps.wires.get(tag)
            if callers is not None:
                a.strong |= maps.mask(callers)
                a.loose |= maps.mask(callers)
                a.specs.append(("wires", [tag], None, 0))
            return True
        if tag not in maps.wired:
            return False  # not a callable entry, or the map does not follow it
        callers = maps.wires.get(tag)
        a.units.append((path, "entry", first, last))
        if callers is not None:
            a.strong |= maps.mask(callers)
            a.loose |= maps.mask(callers)  # the map does not say where in the test it is called
        a.specs.append(("wires", [tag], None, 0))
        a.notes.append(f"entry:{tag}")
        return True

    def _unit(self, a, path, kind, first, last, classes, names):
        if kind == "doc":
            a.notes.append(f"docstring:{path}:{first}")
            return
        a.units.append((path, kind, first, last))
        if kind == "func":
            self._function_unit(a, path, first, last)
        elif self._state is not None and path.startswith("sqlglot/"):
            a.deferred += 1
        else:
            if path.startswith("sqlglot/"):
                a.body_units += 1
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
        a.loose |= maps.lines(maps.loose, path, first, last)
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
            for sub in self.subclasses(class_key):
                k = maps.loose_classes.get(sub)
                if k is not None:
                    a.loose |= maps.mask(k)
            a.strong |= tests
            a.specs.append(("classes", self.subclasses(class_key), None, 0))
            a.notes.append(f"class:{class_key}")
            return
        family = maps.families.get(path)
        if family and BASE_DIALECT in family:
            # A core file: every dialect is built on it, and so are tests that use no
            # dialect at all. Who uses a dialect says nothing about who depends on it.
            family = None
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
        # The value can only shape load-time state if a dialect built from this file
        # is used while sqlglot loads (it parses a few defaults with the base dialect).
        load_time = not family or bool(set(family) & maps.self_dialects)
        if names:
            readers = 0
            for name in names:
                found = self.refs(name)
                if not found:
                    readers = None
                    break
                for ref_path, ref_kind, ref_first, ref_last in found:
                    if ref_kind == "merge":
                        continue  # a subclass copying the table: read through the same name
                    if ref_kind != "func" or (
                            load_time and self._at_load(ref_path, ref_first, ref_last)):
                        readers = None
                        break
                    places.append((ref_path, ref_first, ref_last))
                if readers is not None and not places:
                    readers = None  # only copies, no reader found: cannot pin it down
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
                    a.loose |= maps.lines(maps.loose, ref_path, ref_first, ref_last)
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
            a.loose |= loose_users
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
        # A changed function that this test ran while a module was loading shapes
        # everything the test does afterwards, not only the case or statement that
        # happened to trigger the load.
        load = self.maps.load
        for kind, what, _, _ in a.specs:
            if kind == "reads":
                # A value is often read once and kept (a generator copies its table of
                # transforms the first time its class is used): the case that happened
                # to read it is not the only one that depends on it.
                return None
            if kind == "lines":
                for path, first, last in what:
                    if (self.maps.lines(load, path, first, last) >> bit_index) & 1:
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
                # Such a walk runs as the test's own code, so its cases only matter if
                # the test's own code meets the change.
                result[fixture] = None if a.specs and (a.loose >> bit_index) & 1 else set()
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
                    if family and found:
                        used = 0
                        for key in family:
                            cid = data["dialects"].get(key)
                            if cid is not None:
                                used |= int.from_bytes(sets[cid], "little")
                        found &= used
                else:
                    table = data.get(kind) or {}
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
