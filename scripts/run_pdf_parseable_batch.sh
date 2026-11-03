#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv run sister batch build-pdf-parseable
sister query batch \
  --input inputs/pdf_parseable_batch.csv \
  --command auto \
  --wait \
  --output-dir outputs/pdf_parseable_batch
