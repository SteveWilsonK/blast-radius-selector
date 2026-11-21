"""Loaded by any Python that a test starts as a separate process.

A test that runs `python -c "import sqlglot ..."` must see the patched files too,
so the parent leaves the patch in a file and points BR_OVERLAY at it.
"""
import json
import os
import sys

_spec = os.environ.get("BR_OVERLAY")
if _spec:
    try:
        _app = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _app not in sys.path:
            sys.path.append(_app)
        from br import overlay

        with open(_spec, encoding="utf-8") as _handle:
            _data = json.load(_handle)
        overlay.install(_data["root"], _data["files"])
    except Exception:  # noqa: BLE001 - never break the interpreter that loads this
        pass
