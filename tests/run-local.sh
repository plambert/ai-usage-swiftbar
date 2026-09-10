#!/bin/bash
# End-to-end run against fixtures, no network, no 1Password, no claude:
# starts a fixture HTTP server, runs the daemon once, then renders each
# plugin once from the snapshots it wrote.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
port=${PORT:-18766}
work=$(mktemp -d)
trap 'kill $server 2>/dev/null; rm -rf "$work"' EXIT

python3 -m http.server "$port" --bind 127.0.0.1 --directory "$here/fixtures" >/dev/null 2>&1 &
server=$!
for _ in $(seq 1 50); do
    curl -fs "http://127.0.0.1:$port/api/v1/credits" >/dev/null 2>&1 && break
    sleep 0.1
done

cat > "$work/config.json" <<JSON
{"openrouter": {"key_ref": "op://Fixture/Item/field", "interval": 60},
 "claude": {"interval": 60}}
JSON

echo "== daemon --once"
env OPENROUTER_API_BASE="http://127.0.0.1:$port/api/v1" \
    OPENROUTER_MANAGEMENT_KEY=fixture \
    CLAUDE_USAGE_FIXTURE="$here/fixtures/claude-usage.json" \
    python3 "$root/daemon/ai-usage-daemon.py" --config "$work/config.json" --data-dir "$work/data" --once
echo
for p in openrouter-credits claude-usage; do
    echo "== $p"
    env AI_USAGE_DATA_DIR="$work/data" SWIFTBAR_PLUGIN_DATA_PATH="$work/plugin-$p" \
        python3 "$root/plugins/$p.stream.py" --once | sed 's/templateImage=[A-Za-z0-9+\/=]*/templateImage=…/'
done
