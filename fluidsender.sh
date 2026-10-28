#!/bin/bash
# Starts the FluidSender viewer (if it isn't running) and opens it in its own Chrome window.
# A running server too old for the page is replaced, but only while it holds no serial port:
# one without the machine connection at all (stage 0), or one older than its files and not connected.
PORT=${FLUIDSENDER_PORT:-8765}
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://localhost:$PORT"

stale_server() {
    local code state pid age newest
    code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-FluidSender: 1' "$URL/api/machine/state")
    [ "$code" = 404 ] && return 0
    [ "$code" = 200 ] || return 1
    state=$(curl -s -H 'X-FluidSender: 1' "$URL/api/machine/state")
    [[ "$state" == *'"connected": false'* ]] || return 1
    pid=$(server_pid) || return 1
    age=$(ps -o etimes= -p "$pid" | tr -d ' ')
    [ -n "$age" ] || return 1
    newest=$(stat -c %Y "$DIR/server.py" "$DIR/machine.py" | sort -n | tail -1)
    [ "$newest" -gt $(( $(date +%s) - age )) ]
}

server_pid() {          # the FluidSender server listening on $PORT, if that is what listens there
    local pid
    pid=$(ss -Hltnp "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
    [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'server\.py' && echo "$pid"
}

if curl -s -o /dev/null "$URL/" && stale_server && pid=$(server_pid); then
    kill "$pid"         # (SIGTERM: it closes cleanly)
    for i in $(seq 50); do curl -s -o /dev/null "$URL/" || break; sleep 0.1; done
fi
if ! curl -s -o /dev/null "$URL/"; then
    nohup python3 "$DIR/server.py" --no-browser --port "$PORT" >/dev/null 2>&1 &
    for i in $(seq 20); do curl -s -o /dev/null "$URL/" && break; sleep 0.1; done
fi
exec "${FLUIDSENDER_BROWSER:-google-chrome}" --app="$URL/" --window-size=1280,800 "$@"
