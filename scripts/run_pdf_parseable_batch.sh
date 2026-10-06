#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 scripts/build_pdf_parseable_batch.py
sister query batch \
  --input inputs/pdf_parseable_batch.csv \
  --command auto \
  --wait \
  --output-dir outputs/pdf_parseable_batch
