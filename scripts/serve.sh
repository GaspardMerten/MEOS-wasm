#!/usr/bin/env bash
# Serves web/ on http://localhost:8765 for local development.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${PORT:-8765}"
cd "$ROOT/web"
echo "serving $PWD on http://localhost:$PORT"
exec python3 -m http.server "$PORT"
