#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/tmp/azure-storage-benchmark-smoke}"
mkdir -p "$ROOT"

python3 -m azure_storage_benchmarks run \
  --path "local=$ROOT" \
  --small-files 10 \
  --small-file-size-kib 1 \
  --large-file-size-mib 4 \
  --checkpoint-files 1 \
  --checkpoint-size-mib 2 \
  --iterations 1 \
  --output-json storage-benchmark-results-smoke.json \
  --output-md storage-benchmark-results-smoke.md

