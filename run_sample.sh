#!/bin/sh
# Build the image and run it on a sample set under the scorer's limits, then score it.
# Usage: ./run_sample.sh [sample_dir] [platform]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SAMPLE="${1:-$HERE/../kit/sample}"
PLATFORM="${2:-linux/amd64}"
docker build --platform "$PLATFORM" --tag blast-radius-entry "$HERE" >/dev/null
docker run --rm --platform "$PLATFORM" --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$SAMPLE/commits:/data/commits:ro" \
  --volume "$SAMPLE/tests.json:/data/tests.json:ro" \
  blast-radius-entry > "$HERE/output.jsonl"
python3 "$HERE/../kit/score.py" "$SAMPLE" "$SAMPLE/answers.json" "$HERE/output.jsonl"
