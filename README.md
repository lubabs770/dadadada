# dadadada

A web kanban board for the [herdr](https://github.com/herdrdev/herdr) TUI: every coding agent is a card in a
column by status (Needs you, Working, Waiting, Done, Idle, Terminals), live in your browser at
<http://127.0.0.1:7391/>.

- Live updates from herdr's event socket.
- Click a card for the agent's live terminal (type into it with `i`) or a chat view of a Claude Code session.
- **New** button: start Claude / opencode / copilot or a plain terminal in any space and folder.
- Claude Code statusline data (model, context, cost, rate limits) as a HUD; needs the statusline to save its
  JSON to `~/.local/state/claude-usage/sessions/<session_id>.json`.
- **Waiting** column: Claude agents that look done but still wait for background subagents or commands
  (ported from the [kanban-app](https://github.com/dougcunha/herdr-mods/tree/main/kanban-app) herdr app mod).
- Claude sessions running outside herdr show up too (read-only).
- ⌘K palette, keyboard driven, Iosevka Nerd Font if you have it.

Python standard library only. Listens on 127.0.0.1 and refuses foreign `Host` and `Origin` headers.

## Run

```sh
./kanban.sh start      # or: python3 -I server.py [--port 7391] [--session NAME] [--read-only]
./kanban.sh open
```

As a herdr plugin (starts with the herdr server, adds "Kanban: open board" and "Kanban: restart" actions):

```sh
herdr plugin link /path/to/this/repo
```

## Settings

Optional `local.env` next to `kanban.sh` (not committed):

```sh
export KANBAN_WORK_MATCH=acme          # a Claude login whose email/org contains this is "work"
export KANBAN_WORK_DIR="$HOME/code/acme" # new agents in here default to the work login
export KANBAN_PORT=7391
```

## Test

```sh
python3 -I test_scanner.py
```
