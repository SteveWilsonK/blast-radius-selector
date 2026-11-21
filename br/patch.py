"""Minimal unified-diff reader: which old lines a patch touches, and the new text."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old_path", "new_path", "hunks", "binary")

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []      # (old_start, [(tag, text)])
        self.binary = False

    @property
    def path(self):
        return self.new_path or self.old_path


def _strip(path):
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
        elif cur is not None and line.startswith("rename from "):
            cur.old_path = line[12:].strip()
        elif cur is not None and line.startswith("rename to "):
            cur.new_path = line[10:].strip()
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new_path = None
        elif cur is not None and line.startswith("new file mode"):
            cur.old_path = None
        elif line.startswith(("GIT binary patch", "Binary files ")):
            if cur is not None:
                cur.binary = True
        elif line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            i += 1
        elif cur is not None and (m := HUNK.match(line)):
            old_start = int(m.group(1))
            old_count = 1 if m.group(2) is None else int(m.group(2))
            new_count = 1 if m.group(4) is None else int(m.group(4))
            body = []
            i += 1
            while i < n and (old_count > 0 or new_count > 0):
                h = lines[i]
                tag = h[:1]
                if tag == "\\":
                    i += 1
                    continue
                if tag == "-":
                    old_count -= 1
                elif tag == "+":
                    new_count -= 1
                else:
                    tag = " "
                    old_count -= 1
                    new_count -= 1
                body.append((tag, h[1:]))
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    return files


def touched(file_patch):
    """Old-file line numbers removed, and insertion points (old line the new text lands before)."""
    removed, inserts = [], []
    for old_start, body in file_patch.hunks:
        o = old_start
        if o == 0:
            o = 1
        pending_insert = False
        run_removed = False
        for tag, _ in body:
            if tag == "-":
                removed.append(o)
                o += 1
                run_removed = True
            elif tag == "+":
                if not run_removed:
                    pending_insert = True
            else:
                if pending_insert:
                    inserts.append(o)
                pending_insert = False
                run_removed = False
                o += 1
        if pending_insert:
            inserts.append(o)
    return removed, inserts


def apply(old_text, file_patch):
    """New file text, or None when the patch does not fit the old text."""
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old
    for old_start, body in file_patch.hunks:
        start = max(old_start - 1, 0)
        if start < pos:
            return None
        out.extend(old[pos:start])
        pos = start
        for tag, text in body:
            if tag == " ":
                if pos >= len(old) or old[pos] != text:
                    return None
                out.append(text)
                pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != text:
                    return None
                pos += 1
            else:
                out.append(text)
    out.extend(old[pos:])
    return "\n".join(out)


def line_changes(file_patch):
    """(removed, added): removed is [(old_line, new_position)], added is
    [(new_line, old_position)]. A position is the line on the other side that the
    change sits in front of."""
    removed, added = [], []
    for old_start, body in file_patch.hunks:
        o = old_start or 1
        shift = 0
        # new-side start: the old start plus what earlier hunks added or removed
        n = o + line_changes_shift(file_patch, old_start)
        for tag, _ in body:
            if tag == "-":
                removed.append((o, n))
                o += 1
            elif tag == "+":
                added.append((n, o))
                n += 1
            else:
                o += 1
                n += 1
    return removed, added


def line_changes_shift(file_patch, before_old_start):
    shift = 0
    for old_start, body in file_patch.hunks:
        if old_start >= before_old_start:
            break
        for tag, _ in body:
            if tag == "+":
                shift += 1
            elif tag == "-":
                shift -= 1
    return shift
