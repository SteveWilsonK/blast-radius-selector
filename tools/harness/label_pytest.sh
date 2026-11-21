#!/bin/sh
# Development harness: true failing tests by running real pytest on a patched checkout.
# Usage: label_pytest.sh <clean sqlglot clone> <harness_dir> [jobs]
# Writes <harness_dir>/answers/<id>.json. The clone is reset after every patch.
set -u
CLONE="$1"; H="$2"; JOBS="${3:-4}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$H/answers" "$H/pytest"
for PATCH in "$H"/commits/*.patch; do
  ID="$(basename "$PATCH" .patch)"
  [ -f "$H/answers/$ID.json" ] && continue
  git -C "$CLONE" reset -q --hard && git -C "$CLONE" clean -fdxq
  if [ -s "$PATCH" ] && ! git -C "$CLONE" apply "$PATCH"; then echo "$ID: does not apply"; continue; fi
  docker run --rm --platform linux/arm64 --network none --cpus "$JOBS" \
    -v "$CLONE:/sqlglot" -v "$HERE:/plug:ro" -v "$H/pytest:/out" \
    -e PYTHONPATH=/sqlglot:/plug -e PYTHONDONTWRITEBYTECODE=1 -e OUTCOMES="/out/$ID.json" \
    br-dev-arm python -m pytest tests -q -n "$JOBS" -p outcomes_plugin -p no:cacheprovider \
    --timeout=600 > "$H/pytest/$ID.log" 2>&1
  python3 - "$H" "$ID" <<'PY'
import json, sys, os
h, cid = sys.argv[1], sys.argv[2]
tests = json.load(open(os.path.join(h, "tests.json")))["tests"]
path = os.path.join(h, "pytest", cid + ".json")
if not os.path.exists(path):
    print(cid, "pytest wrote no outcomes"); sys.exit(0)
out = json.load(open(path))
failed = set(out["failed"]); broken = out["broken"]
failing = [t["number"] for t in tests if t["id"] in failed or any(t["id"].startswith(b) for b in broken if b)]
json.dump({"failing": failing, "unknown": []}, open(os.path.join(h, "answers", cid + ".json"), "w"))
print(cid, len(failing), "failing;", "broken files:", broken[:3])
PY
done
git -C "$CLONE" reset -q --hard && git -C "$CLONE" clean -fdxq
