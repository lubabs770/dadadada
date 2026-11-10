#!/bin/sh
# Entry point for the herdr plugin (herdr-plugin.toml). Also works by hand: ./kanban.sh start|open
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
# Optional machine-specific settings (KANBAN_WORK_MATCH, KANBAN_WORK_DIR, KANBAN_PORT).
[ -f ./local.env ] && . ./local.env
PORT="${KANBAN_PORT:-7391}"

case "$1" in
  start)
    # Detach so herdr's startup/action call returns at once; the new server replaces the old one.
    nohup python3 -I server.py --port "$PORT" --replace >> kanban.log 2>&1 &
    ;;
  open)
    open "http://127.0.0.1:$PORT/"
    ;;
  *)
    echo "usage: $0 start|open" >&2
    exit 2
    ;;
esac
