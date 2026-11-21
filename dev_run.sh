#!/bin/sh
# Development run on arm64 (native on Apple Silicon) with the repo mounted.
# Usage: ./dev_run.sh [sample_dir]   (extra env via BR_ENV="-e X=1")
HERE="$(cd "$(dirname "$0")" && pwd)"
SAMPLE="${1:-$HERE/../kit/sample}"
docker run --rm --platform linux/arm64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$SAMPLE/commits:/data/commits:ro" \
  --volume "$SAMPLE/tests.json:/data/tests.json:ro" \
  --volume "$HERE/br:/app/br:ro" --volume "$HERE/maps:/app/maps:ro" \
  --volume "$HERE/../work/sqlglot:/opt/sqlglot:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 -e BR_REPORT=1 $BR_ENV -w /app \
  br-dev-arm python -m br.main > "$HERE/output.jsonl"
python3 "$HERE/../kit/score.py" "$SAMPLE" "$SAMPLE/answers.json" "$HERE/output.jsonl"
