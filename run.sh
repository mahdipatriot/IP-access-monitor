#!/usr/bin/env bash
#
# run.sh — Launch the Check-Host IP Monitor
#
# Loads .env and starts the Python monitor.  Useful for manual runs
# or when not using the systemd service.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if it exists
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

# Use venv if available, otherwise system python3
if [[ -d venv ]]; then
    source venv/bin/activate
fi

exec python3 monitor.py "$@"
