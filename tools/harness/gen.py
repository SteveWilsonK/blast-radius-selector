"""Development harness: make synthetic patches against the pinned sqlglot commit.

    python3 tools/harness/gen.py <sqlglot_checkout> <out_dir> [count] [seed]

Writes <out_dir>/commits/<id>.patch and <out_dir>/kinds.json. The patches are only
used to measure the selector (miss rate, cost); nothing derived from them ships.
"""
import ast
import difflib
import json
import os
import random
import re
import sys

root, out_dir = sys.argv[1], sys.argv[2]
count = int(sys.argv[3]) if len(sys.argv) > 3 else 300
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 7)


def listing(top, suffix):
    found = []
    for folder, _, names in os.walk(os.path.join(root, top)):
        for name in names:
            if name.endswith(suffix) and "__pycache__" not in folder:
                found.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(found)


SOURCE = listing("sqlglot", ".py")
if os.environ.get("HARNESS_ONLY"):
    # e.g. HARNESS_ONLY=sqlglot/executor/ to aim every code edit at one folder
    SOURCE = [p for p in SOURCE if p.startswith(os.environ["HARNESS_ONLY"])]
TESTS = [p for p in listing("tests", ".py") if "/sqlglot/" not in p]
FIXTURES = listing("tests/fixtures", ".sql")
_text = {}


def read(path):
    if path not in _text:
        _text[path] = open(os.path.join(root, path), encoding="utf-8").read()
    return _text[path]


def weighted(paths):
    weights = [len(read(p)) for p in paths]
    return rng.choices(paths, weights=weights)[0]


def diff(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    if not body:
        return None
    if not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{path} b/{path}\n" + body


def parses(text):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def changed(old, new):
    a, b = parses(old), parses(new)
    return b is not None and ast.dump(a) != ast.dump(b)


OPS = [(r"\band\b", "or"), (r"\bor\b", "and"), (r"==", "!="), (r"!=", "=="),
       (r"\bis not\b", "is"), (r"\bis\b(?! not)", "is not"), (r"<=", "<"), (r">=", ">"),
       (r"(?<![<>=!-])<(?![<=])", "<="), (r"(?<![<>=!-])>(?![>=])", ">="),
       (r"\bnot in\b", "in"), (r"\bif not\b", "if"), (r" \+ ", " - "), (r" - ", " + "),
       (r"\bis\b(?! not)", "=="), (r"\bTrue\b", "False"), (r"\bFalse\b", "True")]


def line_edit(path, picker, edit, must_change=True, tries=400):
    old = read(path)
    lines = old.split("\n")
    for _ in range(tries):
        i = picker(lines)
        if i is None:
            return None
        new_line = edit(lines[i])
        if new_line is None or new_line == lines[i]:
            continue
        new = "\n".join(lines[:i] + ([new_line] if new_line != "\0" else []) + lines[i + 1:])
        if must_change and not changed(old, new):
            continue
        return diff(path, old, new)
    return None


def code_line(lines):
    for _ in range(200):
        i = rng.randrange(len(lines))
        s = lines[i].strip()
        if s and not s.startswith(("#", '"""', "'''", "import ", "from ", "@", "class ", "def ")):
            return i
    return None


def edit_op(line):
    options = [(p, r) for p, r in OPS if re.search(p, line.split("#")[0])]
    if not options:
        return None
    pattern, repl = rng.choice(options)
    spans = [m for m in re.finditer(pattern, line)]
    m = rng.choice(spans)
    return line[:m.start()] + repl + line[m.end():]


def edit_const(line):
    numbers = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
    strings = list(re.finditer(r"\"([A-Za-z_ ]{2,})\"", line))
    choice = rng.random()
    if numbers and (choice < 0.5 or not strings):
        m = rng.choice(numbers)
        return line[:m.start()] + str(int(m.group(1)) + rng.choice((1, -1, 2))) + line[m.end():]
    if strings:
        m = rng.choice(strings)
        return line[:m.start(1)] + m.group(1) + "X" + line[m.end(1):]
    return None


def edit_return(line):
    m = re.match(r"^(\s*)return (.+)$", line)
    if not m or m.group(2).strip() in ("None", "self"):
        return None
    return f"{m.group(1)}return None"


def edit_delete(line):
    s = line.strip()
    if not s or s.endswith((":", ",", "(", "[", "{")) or s.startswith(("return", "raise", ")", "]", "}")):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    return indent + "pass"


def edit_arg(line):
    m = re.search(r"seq_get\((\w+), (\d)\)", line)
    if not m:
        return None
    return line[:m.start(2)] + str((int(m.group(2)) + 1) % 3) + line[m.end(2):]


def edit_table_key(line):
    m = re.match(r"^(\s+)(\"[A-Z_ ]+\"|exp\.\w+|TokenType\.\w+|exp\.DType\.\w+)(: .+)$", line)
    if not m:
        return None
    roll = rng.random()
    if roll < 0.4:
        return "\0"  # delete the entry
    if roll < 0.7 and m.group(2).startswith('"'):
        return f"{m.group(1)}{m.group(2)[:-1]}_X\"{m.group(3)}"
    return edit_const(line) or edit_op(line) or "\0"


def edit_attr(line):
    m = re.match(r"^(\s+)([A-Z][A-Z_0-9]+)( = )(True|False|-?\d+|None|\".*\")$", line)
    if not m:
        return None
    value = m.group(4)
    new = {"True": "False", "False": "True", "None": "0"}.get(value)
    if new is None:
        new = str(int(value) + 1) if re.fullmatch(r"-?\d+", value) else value[:-1] + 'x"'
    return m.group(1) + m.group(2) + m.group(3) + new


def edit_comment(line):
    indent = line[: len(line) - len(line.lstrip())]
    return line + "\n" + indent + "# " + rng.choice(["Cheap check first", "See the docs", "Kept for clarity"])


def edit_syntax(line):
    return line + " ("


def edit_sql(line):
    s = line.strip()
    if not s or s.startswith(("#", "--")):
        return None
    words = list(re.finditer(r"[A-Za-z_]{2,}", line))
    if not words:
        return None
    m = rng.choice(words)
    roll = rng.random()
    if roll < 0.5:
        return line[:m.start()] + "x " + line[m.start():]
    if roll < 0.8:
        return line[:m.end()] + "a" + line[m.end():]
    return "\0"


def edit_sql_comment(line):
    s = line.strip()
    if not s or s.startswith(("#", "--")) or ";" not in line:
        return None
    return line.replace(";", " /* note */;", 1) if rng.random() < 0.5 else "-- note\n" + line


def edit_sql_in_string(line):
    m = re.search(r"\"(SELECT [^\"]{6,})\"", line)
    if not m:
        return None
    return line[:m.end(1)] + " /* c */" + line[m.end(1):]


def edit_insert(line):
    s = line.strip()
    if not s or s.endswith((",", "(", "[", "{", "\\")) or s.startswith((")", "]", "}", "#", "@", "elif", "else", "except", "finally")):
        return None
    if s.endswith(":"):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    extra = rng.choice(["_unused = 0", "pass", "_unused = None"])
    return line + "\n" + indent + extra


def edit_docstring(line):
    if '"""' not in line or line.strip() == '"""':
        return None
    return line.replace('"""', '"""Note. ', 1) if line.strip().startswith('"""') else None


def make(kind):
    if kind in ("op", "const", "ret", "del", "arg", "insert"):
        path = weighted(SOURCE)
        edit = {"op": edit_op, "const": edit_const, "ret": edit_return, "del": edit_delete,
                "arg": edit_arg, "insert": edit_insert}[kind]
        return line_edit(path, code_line, edit, must_change=kind != "insert")
    if kind in ("table", "attr"):
        pool = [p for p in SOURCE if p.split("/")[1] in (
            "parsers", "generators", "dialects", "typing", "tokens.py", "parser.py",
            "generator.py", "expressions")]
        return line_edit(weighted(pool), code_line, edit_table_key if kind == "table" else edit_attr)
    if kind == "comment":
        return line_edit(weighted(SOURCE), code_line, edit_comment, must_change=False)
    if kind == "doc":
        return line_edit(weighted(SOURCE), lambda ls: rng.randrange(len(ls)), edit_docstring,
                         must_change=False)
    if kind == "syntax":
        return line_edit(weighted(SOURCE), code_line, edit_syntax, must_change=False)
    if kind in ("fixture", "fixcomment"):
        path = weighted(FIXTURES)
        old = read(path)
        lines = old.split("\n")
        edit = edit_sql if kind == "fixture" else edit_sql_comment
        for _ in range(300):
            i = rng.randrange(len(lines))
            new_line = edit(lines[i])
            if new_line is None:
                continue
            new = "\n".join(lines[:i] + ([new_line] if new_line != "\0" else []) + lines[i + 1:])
            return diff(path, old, new)
        return None
    if kind == "testpy":
        path = weighted([p for p in TESTS if os.path.basename(p).startswith("test_")])
        return line_edit(path, code_line, rng.choice([edit_const, edit_op, edit_sql_in_string]))
    if kind == "helpers":
        return line_edit(rng.choice(["tests/helpers.py", "tests/dialects/test_dialect.py"]), code_line,
                         rng.choice([edit_const, edit_op, edit_return, edit_delete]))
    if kind == "sqlstring":
        return line_edit(weighted(SOURCE + TESTS), code_line, edit_sql_in_string)
    raise ValueError(kind)


WEIGHTS = {"op": 22, "const": 10, "ret": 6, "del": 8, "arg": 3, "insert": 4, "table": 10,
           "attr": 8, "comment": 6, "doc": 2, "syntax": 2, "fixture": 14, "fixcomment": 3,
           "testpy": 5, "helpers": 2, "sqlstring": 2, "multi": 8}
if os.environ.get("HARNESS_KINDS"):
    # e.g. HARNESS_KINDS=op:40,const:20,ret:10,del:10,arg:5,attr:8,table:7
    WEIGHTS = {k: int(v) for k, v in (item.split(":") for item in os.environ["HARNESS_KINDS"].split(","))}
kinds = {}
os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
names = list(WEIGHTS)
made = 0
while made < count:
    kind = rng.choices(names, weights=[WEIGHTS[k] for k in names])[0]
    if kind == "multi":
        parts, seen = [], set()
        for sub in rng.sample(["op", "const", "comment", "fixture", "table", "attr", "del"], 3):
            patch = make(sub)
            if patch:
                path = patch.split("\n", 1)[0]
                if path not in seen:
                    seen.add(path)
                    parts.append(patch)
        patch = "".join(parts) if len(parts) >= 2 else None
    else:
        patch = make(kind)
    if not patch:
        continue
    name = f"{os.environ.get('HARNESS_PREFIX', 'h')}{made:03d}"
    open(os.path.join(out_dir, "commits", name + ".patch"), "w").write(patch)
    kinds[name] = kind
    made += 1
json.dump(kinds, open(os.path.join(out_dir, "kinds.json"), "w"), indent=0)
print(f"{made} patches in {out_dir}", file=sys.stderr)
