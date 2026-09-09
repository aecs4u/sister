#!/usr/bin/env bash
# =============================================================================
# visura-api CLI — usage examples
#
# These examples assume the visura-api service is running at the URL
# configured in VISURA_API_URL (default: http://localhost:8000).
#
# Start the service first:
#   uvicorn main:app --host 0.0.0.0 --port 8000
#
# Install the CLI:
#   pip install -e .        # or: uv pip install -e .
# =============================================================================

set -euo pipefail

# --- Configuration (override via environment) --------------------------------
# export VISURA_API_URL=http://localhost:8000
# export VISURA_API_KEY=your-secret-key

# =============================================================================
# 1. List available endpoints
# =============================================================================

visura-api queries

# =============================================================================
# 2. Check service health
# =============================================================================

visura-api health

# =============================================================================
# 3. Submit a search — Fabbricati only
# =============================================================================

# Dry run first to preview what will be sent
visura-api search \
    --provincia Trieste \
    --comune TRIESTE \
    --foglio 9 \
    --particella 166 \
    --tipo-catasto F \
    --dry-run

# Submit for real (returns request IDs immediately)
visura-api search \
    -P Trieste \
    -C TRIESTE \
    -F 9 \
    -p 166 \
    -t F

# =============================================================================
# 4. Submit a search — both Terreni + Fabbricati, wait for results
# =============================================================================

# Omit --tipo-catasto to search both; --wait polls until done
visura-api search \
    -P Trieste \
    -C TRIESTE \
    -F 9 \
    -p 166 \
    --wait

# =============================================================================
# 5. Submit and save results to a file
# =============================================================================

visura-api search \
    -P Roma \
    -C ROMA \
    -F 100 \
    -p 50 \
    -t T \
    --wait \
    --output results_roma.json

# =============================================================================
# 6. Poll a specific request ID
# =============================================================================

# Replace with an actual request ID from a previous search
# visura-api get req_F_abc123

# =============================================================================
# 7. Wait for a specific request (with custom timeout)
# =============================================================================

# visura-api wait req_F_abc123 --timeout 600 --interval 3

# =============================================================================
# 8. Look up owners (intestati) for a specific subalterno
# =============================================================================

# Fabbricati require --subalterno
visura-api intestati \
    -P Trieste \
    -C TRIESTE \
    -F 9 \
    -p 166 \
    -t F \
    -sub 3 \
    --dry-run

# With --wait to get the result inline
# visura-api intestati \
#     -P Trieste -C TRIESTE -F 9 -p 166 -t F -sub 3 --wait

# Terreni (no subalterno)
visura-api intestati \
    -P Roma \
    -C ROMA \
    -F 100 \
    -p 50 \
    -t T \
    --dry-run

# =============================================================================
# 9. Query history with filters
# =============================================================================

# All history (last 50)
visura-api history

# Filtered by province and type
visura-api history --provincia Trieste --tipo-catasto F --limit 20

# Save history to file
visura-api history --provincia Roma --output history_roma.json

# =============================================================================
# 10. Scripting: submit + poll in a loop
# =============================================================================

# JSON output can be piped to jq for automation:
# visura-api search -P Trieste -C TRIESTE -F 9 -p 166 -t F 2>/dev/null \
#     | jq -r '.request_ids[]' \
#     | while read -r rid; do
#         visura-api wait "$rid" --timeout 300 --output "result_${rid}.json"
#     done
