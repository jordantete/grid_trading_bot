#!/usr/bin/env bash
#
# replay_logs.sh — Copy a bot log file into ./logs/ and start the monitoring stack
# so Promtail ingests it and Grafana can visualize it.
#
# Usage:
#   ./scripts/replay_logs.sh /path/to/bot_SOL_USDT_LIVE_*.log
#   ./scripts/replay_logs.sh --cleanup
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$PROJECT_ROOT/logs"
MARKER_FILE="$LOGS_DIR/.replay_marker"

cleanup() {
    echo "==> Tearing down monitoring stack..."
    docker-compose -f "$PROJECT_ROOT/docker-compose.yml" down

    if [[ -f "$MARKER_FILE" ]]; then
        echo "==> Removing replayed log files..."
        while IFS= read -r file; do
            if [[ -f "$file" ]]; then
                rm -v "$file"
            fi
        done < "$MARKER_FILE"
        rm -f "$MARKER_FILE"
    else
        echo "    No replay marker found — nothing to remove."
    fi

    echo "==> Cleanup complete."
}

usage() {
    echo "Usage:"
    echo "  $0 <log-file> [<log-file> ...]   Copy log(s) to ./logs/ and start monitoring stack"
    echo "  $0 --cleanup                      Tear down stack and remove copied logs"
    exit 1
}

if [[ $# -eq 0 ]]; then
    usage
fi

if [[ "$1" == "--cleanup" ]]; then
    cleanup
    exit 0
fi

# Ensure logs directory exists
mkdir -p "$LOGS_DIR"

# Copy each provided log file
for log_file in "$@"; do
    if [[ ! -f "$log_file" ]]; then
        echo "Error: File not found: $log_file" >&2
        exit 1
    fi

    filename="$(basename "$log_file")"
    dest="$LOGS_DIR/$filename"

    if [[ -f "$dest" ]]; then
        echo "    Log already exists: $dest (skipping copy)"
    else
        cp -v "$log_file" "$dest"
    fi

    # Track copied files for cleanup
    echo "$dest" >> "$MARKER_FILE"
done

# Start the monitoring stack
echo "==> Starting monitoring stack..."
docker-compose -f "$PROJECT_ROOT/docker-compose.yml" up -d

echo ""
echo "==> Waiting for services to initialize..."
sleep 5

echo ""
echo "====================================="
echo "  Grafana: http://localhost:3000"
echo "  Default credentials: admin / admin"
echo "====================================="
echo ""
echo "To tear down and remove replayed logs:"
echo "  $0 --cleanup"
