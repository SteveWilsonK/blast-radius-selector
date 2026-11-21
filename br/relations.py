"""What a comparison would return, given how its operands were seen to relate.

A relation code packs four observations of one evaluation: same object (0/1),
equal (0/1/2), less (0/1/2), greater (0/1/2), where 2 means it was not observed.
"""
import ast

UNKNOWN = 2


def unpack(code):
    return code % 2, (code // 2) % 3, (code // 6) % 3, (code // 18) % 3


def _either(a, b):
    if a == 1 or b == 1:
        return 1
    if a == 0 and b == 0:
        return 0
    return UNKNOWN


def outcome(op, code):
    """Truth of `left op right` for one observed relation: 0, 1 or UNKNOWN."""
    same, equal, less, more = unpack(code)
    if op is ast.Is:
        return same
    if op is ast.IsNot:
        return 1 - same
    if op is ast.Eq:
        return equal
    if op is ast.NotEq:
        return UNKNOWN if equal == UNKNOWN else 1 - equal
    if op is ast.Lt:
        return less
    if op is ast.Gt:
        return more
    if op is ast.LtE:
        return _either(less, equal)
    if op is ast.GtE:
        return _either(more, equal)
    return UNKNOWN


OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def differs(old_op, new_op, code):
    """Could swapping the operator change the result for this observed relation?"""
    before, after = outcome(old_op, code), outcome(new_op, code)
    return before == UNKNOWN or after == UNKNOWN or before != after


def operator_swap(old_statement, new_statement):
    """If two statements differ only in the operator of one simple comparison,
    return (line, column, old operator type, new operator type) of it in the old one."""
    found = []

    def walk(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Compare):
            if len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
                if ast.dump(a.left) == ast.dump(b.left) and \
                        ast.dump(a.comparators[0]) == ast.dump(b.comparators[0]):
                    found.append((a.lineno, a.col_offset, type(a.ops[0]), type(b.ops[0])))
                    return True
                return False
        fields_a = list(ast.iter_fields(a))
        fields_b = list(ast.iter_fields(b))
        if len(fields_a) != len(fields_b):
            return False
        for (name_a, value_a), (name_b, value_b) in zip(fields_a, fields_b):
            if name_a != name_b:
                return False
            if isinstance(value_a, ast.AST):
                if not isinstance(value_b, ast.AST) or not walk(value_a, value_b):
                    return False
            elif isinstance(value_a, list):
                if not isinstance(value_b, list) or len(value_a) != len(value_b):
                    return False
                for item_a, item_b in zip(value_a, value_b):
                    if isinstance(item_a, ast.AST):
                        if not isinstance(item_b, ast.AST) or not walk(item_a, item_b):
                            return False
                    elif item_a != item_b:
                        return False
            elif value_a != value_b:
                return False
        return True

    if old_statement is None or new_statement is None or not walk(old_statement, new_statement):
        return None
    if len(found) != 1 or found[0][2] not in OPS or found[0][3] not in OPS:
        return None
    return found[0]


# ---- and / or ------------------------------------------------------------
# Truth pairs recorded per evaluation of `x and y` / `x or y` (tools/cond_plugin.py):
# 0 TT, 1 TF, 2 FT, 3 FF with both known, 4 x false and y unknown, 5 x true and y
# unknown. Swapping the operator changes the truth of the whole expression in these:
BOOL_DIFFERS = {ast.And: (1, 2, 4), ast.Or: (1, 2, 5)}


PURE_CALLS = {"isinstance", "len", "bool", "type", "hasattr", "callable", "getattr"}
PURE_METHODS = {"get", "startswith", "endswith", "is_type", "isupper", "islower", "isdigit",
                "upper", "lower", "strip", "find_ancestor"}


def pure(node):
    """Can this expression be evaluated, or skipped, without changing anything else?
    (The same test as tools/cond_plugin.py uses to evaluate a skipped operand.)"""
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return pure(node.value)
    if isinstance(node, ast.Subscript):
        return pure(node.value) and pure(node.slice)
    if isinstance(node, ast.Compare):
        return pure(node.left) and all(pure(c) for c in node.comparators)
    if isinstance(node, ast.UnaryOp):
        return pure(node.operand)
    if isinstance(node, ast.BoolOp):
        return all(pure(v) for v in node.values)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(pure(e) for e in node.elts)
    if isinstance(node, ast.Call):
        if node.keywords and not all(pure(k.value) for k in node.keywords):
            return False
        if not all(pure(a) for a in node.args):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in PURE_CALLS
        if isinstance(node.func, ast.Attribute):
            return node.func.attr in PURE_METHODS and pure(node.func.value)
    return False


def _bool_context(node, parents):
    """Is only the truth of this expression used (not its value)?"""
    parent = parents.get(node)
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and parent.test is node:
        return True
    if isinstance(parent, ast.comprehension) and node in parent.ifs:
        return True
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return True
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and \
            parent.func.id == "bool" and parent.args == [node]:
        return True
    if isinstance(parent, ast.BoolOp):
        return _bool_context(parent, parents)
    return False


def bool_swap(old_statement, new_statement):
    """If two statements differ only in `and` versus `or` of one two-operand boolean
    expression whose truth alone is used, return (line, column, old op, new op) of it
    in the old one, else None."""
    if old_statement is None or new_statement is None:
        return None
    found = []

    def walk(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.BoolOp) and type(a.op) is not type(b.op):
            if len(a.values) == 2 and len(b.values) == 2 and all(
                    ast.dump(x) == ast.dump(y) for x, y in zip(a.values, b.values)):
                found.append(a)
                return True
            return False
        fields_a = list(ast.iter_fields(a))
        fields_b = list(ast.iter_fields(b))
        if len(fields_a) != len(fields_b):
            return False
        for (name_a, value_a), (name_b, value_b) in zip(fields_a, fields_b):
            if name_a != name_b:
                return False
            if isinstance(value_a, ast.AST):
                if not isinstance(value_b, ast.AST) or not walk(value_a, value_b):
                    return False
            elif isinstance(value_a, list):
                if not isinstance(value_b, list) or len(value_a) != len(value_b):
                    return False
                for item_a, item_b in zip(value_a, value_b):
                    if isinstance(item_a, ast.AST):
                        if not isinstance(item_b, ast.AST) or not walk(item_a, item_b):
                            return False
                    elif item_a != item_b:
                        return False
            elif value_a != value_b:
                return False
        return True

    if not walk(old_statement, new_statement) or len(found) != 1:
        return None
    node = found[0]
    second = node.values[1]
    if any(isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)) or
           (isinstance(n, ast.Name) and n.id == "super") for n in ast.walk(second)):
        return None   # the tracer leaves these alone
    parents = {}
    for parent in ast.walk(old_statement):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    if not _bool_context(node, parents):
        return None
    if not pure(second):
        # The swap also changes whether the second operand runs at all, and running
        # it does something (advances a parser, fills a cache...): every evaluation
        # can behave differently, whatever the truths were.
        return None
    new_op = ast.Or if isinstance(node.op, ast.And) else ast.And
    return node.lineno, node.col_offset, type(node.op), new_op
