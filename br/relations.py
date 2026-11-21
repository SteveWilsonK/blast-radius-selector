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
