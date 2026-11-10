#!/usr/bin/env python3
"""Herdr Kanban: a web board for the herdr TUI. Standard library only.

Talks to the herdr API socket directly: one connection subscribes to events, short
connections do requests. The page gets the board and the live terminal over SSE.

Usage: python3 -I server.py [--port 7391] [--session NAME] [--read-only]
Only listens on 127.0.0.1. Rejects foreign Host and Origin headers.
"""
import argparse, glob, json, os, re, shutil, socket, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=7391)
ap.add_argument("--session", default=None, help="herdr session name (default session if omitted)")
ap.add_argument("--read-only", action="store_true", help="refuse keyboard input to panes")
ap.add_argument("--replace", action="store_true", help="stop the kanban started before this one (plugin startup)")
ARGS = ap.parse_args()
PIDFILE = Path(__file__).parent / ".kanban.pid"

HERE = Path(__file__).parent
ALLOWED_HOSTS = {f"127.0.0.1:{ARGS.port}", f"localhost:{ARGS.port}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
PANE_RE = re.compile(r"^[A-Za-z0-9]+:p[0-9]+$")
KEY_RE = re.compile(r"^((ctrl|alt|shift)\+)*[a-z0-9]{1,10}$")


def socket_path():
    # Started by herdr as a plugin: herdr says which session's socket to use.
    if ARGS.session is None and os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    out = subprocess.run(["herdr", "session", "list", "--json"], capture_output=True, text=True, timeout=10).stdout
    for s in json.loads(out)["sessions"]:
        if (ARGS.session is None and s["default"]) or s["name"] == ARGS.session:
            if not s["running"]:
                raise SystemExit(f"herdr session {s['name']!r} is not running")
            return s["socket_path"]
    raise SystemExit(f"no herdr session named {ARGS.session!r}")


SOCK = socket_path()


class HerdrError(Exception):
    pass


def call(method, params=None, timeout=10):
    """One request over a fresh socket connection (newline-delimited JSON)."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(SOCK)
        s.sendall((json.dumps({"id": "k", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    msg = json.loads(buf)
    if "error" in msg:
        raise HerdrError(msg["error"].get("message", str(msg["error"])))
    return msg["result"]


# ---------- Waiting detection (port of the kanban-app mod's waiting.js) ----------

WAITING_TTL_MS = 60 * 60 * 1000
MAX_TRANSCRIPT = 96 * 1024 * 1024
INTERIM = re.compile(r"waiting on its own background work")
NOTIFY_RECORD = re.compile(r'^\{"type":"queue-operation".*?"content":"<task-notification>|"message":\{"role":"user","content":"<task-notification>|"attachment":\{"type":"queued_command","prompt":"<task-notification>')
SHELL_STARTED = re.compile(r"Command running in background with ID:\s*([A-Za-z0-9_-]+)")
AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
STOP_TOOLS = {"TaskStop", "KillShell", "KillBash"}
CLAUDE_DIRS = [d for d in glob.glob(os.path.expanduser("~/.claude*")) if os.path.isdir(d) and (os.path.isdir(f"{d}/projects") or os.path.isdir(f"{d}/sessions"))]


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b if isinstance(b, str) else (b or {}).get("text", "") for b in content)
    return ""


def scan_transcript(path):
    """Background subagents and shell commands that have not finished: [{id, kind, description, since}]."""
    calls, jobs = {}, {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith('{"type":"api-request'):
                continue
            if '"type":"tool_use"' in line and any(m in line for m in ('"name":"Agent"', '"name":"Task"', '"name":"Bash"', *(f'"name":"{n}"' for n in STOP_TOOLS))):
                try:
                    blocks = json.loads(line).get("message", {}).get("content") or []
                except Exception:
                    continue
                for b in blocks if isinstance(blocks, list) else []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    inp = b.get("input") or {}
                    desc = str(inp.get("description", "")).strip()
                    if b.get("name") in ("Agent", "Task"):
                        calls[b.get("id")] = ("agent", desc)
                    elif b.get("name") == "Bash" and inp.get("run_in_background") is True:
                        calls[b.get("id")] = ("shell", desc)
                    elif b.get("name") in STOP_TOOLS:
                        job = jobs.get(str(inp.get("task_id") or inp.get("shell_id") or ""))
                        if job:
                            job["pending"] = False
            elif '"tool_result"' in line and ("Async agent launched" in line or "Command running in background" in line):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                since = _iso_ms(rec.get("timestamp"))
                blocks = rec.get("message", {}).get("content") or []
                for b in blocks if isinstance(blocks, list) else []:
                    c = calls.get(b.get("tool_use_id")) if isinstance(b, dict) and b.get("type") == "tool_result" else None
                    if not c:
                        continue
                    m = (AGENT_ID if c[0] == "agent" else SHELL_STARTED).search(_text(b.get("content")))
                    if m:
                        jobs[m.group(1)] = {"kind": c[0], "desc": c[1], "since": since, "last": since, "pending": True}
            elif "<task-notification>" in line and NOTIFY_RECORD.search(line):
                m = re.search(r'"timestamp":"([^"]+)"', line)
                at = _iso_ms(m.group(1) if m else None)
                for block in re.findall(r"<task-notification>[\s\S]*?</task-notification>", line):
                    idm = re.search(r"<task-id>([^<\s]+)</task-id>", block)
                    job = jobs.get(idm.group(1)) if idm else None
                    if job:
                        job["pending"] = job["kind"] == "agent" and bool(INTERIM.search(block))
                        job["last"] = max(job["last"], at)
    return [{"id": k, "kind": j["kind"], "description": j["desc"], "since": j["last"] or j["since"]} for k, j in jobs.items() if j["pending"]]


def _iso_ms(s):
    if not s:
        return 0
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


_transcripts = {}  # session id -> (path, mtime, size, items)


def waiting_for(session_id):
    """Pending background jobs of a Claude session, or [] when unknown."""
    if not re.fullmatch(r"[0-9a-f-]{20,40}", session_id or ""):
        return []
    cached = _transcripts.get(session_id)
    path = cached[0] if cached else next((p for d in CLAUDE_DIRS for p in glob.glob(f"{d}/projects/*/{session_id}.jsonl")), None)
    if not path:
        return []
    try:
        st = os.stat(path)
    except OSError:
        _transcripts.pop(session_id, None)
        return []
    if st.st_size > MAX_TRANSCRIPT:
        return []
    if not cached or cached[1:3] != (st.st_mtime, st.st_size):
        # ponytail: rescans the whole file on change; tail-only parsing if big transcripts get slow
        cached = (path, st.st_mtime, st.st_size, scan_transcript(path))
        _transcripts[session_id] = cached
    items = [dict(i) for i in cached[3]]
    reg = registry().get(session_id)
    if reg and reg.get("status") == "shell":
        live_since = reg.get("statusUpdatedAt") or 0
        shells = [i for i in items if i["kind"] == "shell"]
        for i in shells:
            i["live"] = True
        if not shells:
            items.append({"id": "shell", "kind": "shell", "description": "", "since": live_since, "live": True})
    now = time.time() * 1000
    return [i for i in items if i.get("live") or not i["since"] or now - i["since"] < WAITING_TTL_MS]


_registry = {"at": 0, "data": {}}


def registry():
    """Claude Code's live session registry: sessionId -> record. Reads only <dir>/sessions/*.json."""
    if time.time() - _registry["at"] > 2:
        data = {}
        for d in CLAUDE_DIRS:
            for p in glob.glob(f"{d}/sessions/*.json"):
                try:
                    r = json.loads(Path(p).read_text())
                    r["_dir"] = d
                    data[r.get("sessionId")] = r
                except Exception:
                    pass
        _registry.update(at=time.time(), data=data)
    return _registry["data"]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def transcript_tail(session_id, limit=40):
    """The last user/assistant text messages of a Claude session, ANSI-colored for the terminal panel."""
    path = next((p for d in CLAUDE_DIRS for p in glob.glob(f"{d}/projects/*/{session_id}.jsonl")), None)
    if not path:
        return "(no transcript found)"
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 4 * 1024 * 1024))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    out = []
    for line in lines:
        if '"message"' not in line or line.startswith('{"type":"api-request'):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("isSidechain") or rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
            continue
        content = rec.get("message", {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if not text or text.startswith("<"):  # skip tool plumbing, command wrappers, notifications
            continue
        if rec["type"] == "user":
            out.append("\x1b[1;36m❯ " + text.replace("\n", "\n  ") + "\x1b[0m")
        else:
            out.append("\x1b[38;5;252m● " + text.replace("\n", "\n  ") + "\x1b[0m")
    return "\n\n".join(out[-limit:]) or "(no messages yet)"


def find_transcript(session_id):
    if not re.fullmatch(r"[0-9a-f-]{20,40}", session_id or ""):
        return None
    hit = _transcripts.get(session_id)
    if hit and os.path.exists(hit[0]):
        return hit[0]
    return next((p for d in CLAUDE_DIRS for p in glob.glob(f"{d}/projects/*/{session_id}.jsonl")), None)


# ---------- Accounts (Claude config dirs) ----------

HOME = os.path.expanduser("~")
# Which Claude login counts as "work", and the folder whose sessions default to it.
# Set in local.env (not committed), e.g. KANBAN_WORK_MATCH=acme KANBAN_WORK_DIR=~/code/acme
WORK_MATCH = os.environ.get("KANBAN_WORK_MATCH", "").lower()
WORK_DIR = os.path.realpath(os.path.expanduser(os.environ["KANBAN_WORK_DIR"])) if os.environ.get("KANBAN_WORK_DIR") else ""


def _load_accounts():
    """Every Claude config dir with a login. The default ~/.claude keeps its login in ~/.claude.json."""
    out = []
    for d in sorted(CLAUDE_DIRS):
        default = os.path.realpath(d) == os.path.realpath(f"{HOME}/.claude")
        try:
            acct = json.loads(Path(f"{HOME}/.claude.json" if default else f"{d}/.claude.json").read_text()).get("oauthAccount") or {}
        except Exception:
            continue
        email, org = acct.get("emailAddress") or "", acct.get("organizationName") or ""
        if not email:
            continue
        work = bool(WORK_MATCH) and WORK_MATCH in (email + org).lower()
        out.append({"id": Path(d).name, "dir": d, "email": email, "kind": "work" if work else "personal", "default": default})
    return out


ALL_ACCOUNTS = _load_accounts()
KIND_BY_DIR = {a["dir"]: a["kind"] for a in ALL_ACCOUNTS}
# For the New dialog: one entry per login, preferring a named profile over the default dir.
ACCOUNTS = list({a["email"]: a for a in sorted(ALL_ACCOUNTS, key=lambda a: not a["default"])}.values())


def account_kind(config_dir):
    return KIND_BY_DIR.get(config_dir) if config_dir else None


def default_account(cwd):
    want = "work" if WORK_DIR and os.path.realpath(cwd or "").startswith(WORK_DIR) else "personal"
    return next((a["id"] for a in ACCOUNTS if a["kind"] == want), ACCOUNTS[0]["id"] if ACCOUNTS else None)


# ---------- HUD: the statusline data of a Claude session ----------

STATUS_DIR = Path(HOME) / ".local/state/claude-usage/sessions"
_hud = {}  # session id -> (key, hud)


def _ctx_from_transcript(path):
    """Fallback when there is no statusline snapshot: last assistant usage in the transcript."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 512 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    for line in reversed(tail):
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message") or {}
        u = msg.get("usage") or {}
        used = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if used:
            return {"model": msg.get("model"), "ctxUsed": used}
    return {}


def hud_for(session_id):
    """Model, context use, cost, lines and rate limits of a session, or None."""
    if not re.fullmatch(r"[0-9a-f-]{20,40}", session_id or ""):
        return None
    snap = STATUS_DIR / f"{session_id}.json"
    tpath = find_transcript(session_id)
    try:
        key = (snap.stat().st_mtime if snap.exists() else 0, os.path.getmtime(tpath) if tpath else 0)
    except OSError:
        key = (0, 0)
    hit = _hud.get(session_id)
    if hit and hit[0] == key:
        return hit[1]
    hud = {}
    if snap.exists():
        try:
            d = json.loads(snap.read_text())
            cw, cost = d.get("context_window") or {}, d.get("cost") or {}
            cur = cw.get("current_usage")
            used = sum(cur.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) if isinstance(cur, dict) else cur
            rl = d.get("rate_limits") or {}
            hud = {
                "model": (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id"),
                "ctxPct": cw.get("used_percentage"), "ctxUsed": used, "ctxMax": cw.get("context_window_size"),
                "cost": cost.get("total_cost_usd"), "durationMs": cost.get("total_duration_ms"),
                "apiMs": cost.get("total_api_duration_ms"),
                "linesAdded": cost.get("total_lines_added"), "linesRemoved": cost.get("total_lines_removed"),
                "rate5h": rl.get("five_hour"), "rate7d": rl.get("seven_day"),
                "version": d.get("version"), "style": (d.get("output_style") or {}).get("name"),
                "at": int(snap.stat().st_mtime * 1000),
            }
        except Exception:
            hud = {}
    if tpath and not hud.get("ctxUsed"):
        hud = {**_ctx_from_transcript(tpath), **{k: v for k, v in hud.items() if v is not None}}
    hud = hud or None
    _hud[session_id] = (key, hud)
    return hud


# ---------- Chat view: a transcript as chat messages ----------

def _tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    for k in {"Bash": ("description", "command"), "Read": ("file_path",), "Edit": ("file_path",), "Write": ("file_path",),
              "MultiEdit": ("file_path",), "NotebookEdit": ("notebook_path",), "Grep": ("pattern",), "Glob": ("pattern",),
              "Agent": ("description",), "Task": ("description",), "WebFetch": ("url",), "WebSearch": ("query",),
              "Skill": ("skill",), "TaskStop": ("task_id",)}.get(name, ()):
        v = inp.get(k)
        if v:
            v = str(v)
            return Path(v).name if k.endswith("_path") else v.splitlines()[0][:120]
    v = next((v for v in inp.values() if isinstance(v, str) and v), "")
    return v.splitlines()[0][:80] if v else ""


def chat_items(path, limit=200):
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 6 * 1024 * 1024))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    items, tools = [], {}

    def add(role, text="", ts=None):
        items.append({"role": role, "text": text, "tools": [], "ts": ts})
        return items[-1]

    for line in lines:
        if '"message"' not in line or line.startswith('{"type":"api-request'):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("isSidechain") or rec.get("isMeta") or rec.get("type") not in ("user", "assistant"):
            continue
        ts, content = rec.get("timestamp"), (rec.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content if isinstance(content, list) else [])
        if rec["type"] == "user":
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    t = tools.get(b.get("tool_use_id"))
                    if t is not None:
                        t["state"] = "error" if b.get("is_error") else "ok"
                elif b.get("type") == "image":
                    add("user", "🖼 *image*", ts)
                elif b.get("type") == "text":
                    text = (b.get("text") or "").strip()
                    m = re.match(r"<command-name>(.*?)</command-name>", text, re.S)
                    if m:
                        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
                        add("command", (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip(), ts)
                    elif text.startswith("<task-notification>"):
                        s = re.search(r"<summary>(.*?)</summary>", text, re.S)
                        add("event", s.group(1).strip() if s else "background task finished", ts)
                    elif text.startswith("<bash-input>"):
                        add("command", "! " + re.sub(r"</?bash-input>", "", text).strip(), ts)
                    elif text and not text.startswith("<"):
                        add("user", re.sub(r"\[Image #\d+\]\s*", "🖼 ", text), ts)
        else:
            cur = items[-1] if items and items[-1]["role"] == "assistant" else add("assistant", "", ts)
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    cur["text"] = (cur["text"] + "\n\n" + b["text"].strip()).strip()
                elif b.get("type") == "tool_use":
                    t = {"name": b.get("name"), "summary": _tool_summary(b.get("name"), b.get("input")), "state": "run"}
                    tools[b.get("id")] = t
                    cur["tools"].append(t)
            cur["ts"] = ts
    return [i for i in items if i["text"] or i["tools"]][-limit:]


# ---------- Creating tabs, shells and agents ----------

AGENT_KINDS = [k for k in ("claude", "codex", "opencode", "gemini", "amp", "cursor", "copilot", "pi")
               if shutil.which(k) or (k == "claude" and os.path.exists(f"{HOME}/.local/bin/claude"))]
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def folders():
    out = []
    for pat in (f"{HOME}/code/*", *([f"{WORK_DIR}/*"] if WORK_DIR else [])):
        out += [p for p in sorted(glob.glob(pat)) if os.path.isdir(p) and not Path(p).name.startswith(".")]
    return out


def safe_dir(path):
    p = os.path.realpath(os.path.expanduser(path or HOME))
    if not os.path.isdir(p) or not (p == HOME or p.startswith(HOME + os.sep)):
        raise ValueError("folder must be an existing folder inside your home folder")
    return p


def unique_name(base, taken):
    base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-_") or "agent"
    if not base[0].isalpha():
        base = "a-" + base
    base = base[:26]
    for n in range(1, 100):
        name = base if n == 1 else f"{base}-{n}"
        if name not in taken:
            return name
    raise ValueError("no free agent name")


def create(body):
    kind = body.get("kind")
    if kind != "shell" and kind not in AGENT_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    cwd = safe_dir(body.get("cwd"))
    folder = Path(cwd).name or "~"
    env = {}
    if kind == "claude":
        acct = next((a for a in ACCOUNTS if a["id"] == (body.get("account") or default_account(cwd))), None)
        if acct:
            env = {"CLAUDE_CONFIG_DIR": acct["dir"], "CLAUDE_PICK": "0"}
    taken = {a.get("name") for a in call("agent.list")["agents"]}
    name = body.get("name") or None
    if kind != "shell":
        if name and not NAME_RE.match(name):
            raise ValueError("name must match [a-z][a-z0-9_-]{0,31}")
        if name in taken:
            raise ValueError(f"an agent named {name} already runs")
        name = name or unique_name(folder, taken)
    label = name if kind != "shell" else f"shell · {folder}"
    ws = body.get("workspace") or None
    if ws:
        if not re.fullmatch(r"[A-Za-z0-9]+", ws):
            raise ValueError("bad space id")
        res = call("tab.create", {"workspace_id": ws, "cwd": cwd, "label": label, "env": env, "focus": False})
    else:
        res = call("workspace.create", {"cwd": cwd, "label": body.get("space") or folder, "env": env, "focus": False})
        call("tab.rename", {"tab_id": res["tab"]["tab_id"], "label": label})
    pane = res["root_pane"]["pane_id"]
    call("pane.rename", {"pane_id": pane, "label": label})
    note = None
    if kind != "shell":
        try:
            call("agent.start", {"name": name, "kind": kind, "pane_id": pane, "timeout_ms": 45000}, timeout=60)
            # The API returns before the agent is up (the CLI waits): wait until it is ready for input.
            deadline, status = time.time() + 45, None
            while time.time() < deadline:
                status = next((a.get("agent_status") for a in call("agent.list")["agents"] if a["pane_id"] == pane), None)
                if status in ("idle", "done", "blocked"):
                    break
                time.sleep(0.4)
            if status == "blocked":
                raise HerdrError("it's asking something on startup (trust dialog?): answer it in the terminal")
            if (body.get("prompt") or "").strip():
                call("agent.prompt", {"target": pane, "text": body["prompt"].strip()})
        except HerdrError as e:
            note = str(e)  # e.g. stuck on a trust dialog: the pane is there, answer it in the terminal
    return {"pane": pane, "name": name, "note": note}


# ---------- Git branch (reads .git/HEAD, no subprocess) ----------

_branches = {}


def git_branch(cwd):
    hit = _branches.get(cwd)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    branch, d = None, Path(cwd or "/")
    for p in [d, *d.parents]:
        g = p / ".git"
        try:
            if g.is_file():  # worktree: "gitdir: <path>"
                g = Path(g.read_text().split(":", 1)[1].strip())
            head = (g / "HEAD").read_text().strip() if g.exists() else None
        except Exception:
            head = None
        if head:
            branch = head[16:] if head.startswith("ref: refs/heads/") else head[:8]
            break
    _branches[cwd] = (time.time(), branch)
    return branch


# ---------- Board state ----------

class Board:
    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.json = "{}"
        self.error = None
        self.since = {}  # pane_id -> (status, ms)
        self.dismissed = {}  # pane_id -> completion_seq when "not waiting" was clicked
        self.pane_ids = set()
        self.sessions = set()
        self.dirty = threading.Event()

    def build(self):
        snap = call("session.snapshot")["snapshot"]
        ws = {w["workspace_id"]: w for w in snap["workspaces"]}
        tabs = {t["tab_id"]: t for t in snap["tabs"]}
        panes = {p["pane_id"]: p for p in snap["panes"]}
        rects = {p["pane_id"]: p["rect"] for l in snap.get("layouts", []) for p in l["panes"]}
        now = int(time.time() * 1000)
        cards = []
        for a in snap["agents"]:
            pid, status = a["pane_id"], a.get("agent_status") or "unknown"
            prev = self.since.get(pid)
            if not prev or prev[0] != status:
                self.since[pid] = (status, now)
            waiting = []
            seq = a.get("completion_seq")
            if a.get("agent") == "claude" and status in ("done", "idle") and self.dismissed.get(pid) != seq:
                sess = (a.get("agent_session") or {}).get("value")
                try:
                    waiting = waiting_for(sess)
                except Exception:
                    waiting = []
            pane = panes.get(pid, {})
            rect = rects.get(pid) or {}
            cards.append({
                "pane": pid,
                "agent": a.get("agent"),
                "status": "waiting" if waiting else status,
                "herdrStatus": status,
                "since": self.since[pid][1],
                "workspace": ws.get(a["workspace_id"], {}).get("label") or a["workspace_id"],
                "workspaceId": a["workspace_id"],
                "tab": tabs.get(a["tab_id"], {}).get("number"),
                "label": pane.get("label"),
                "title": a.get("terminal_title_stripped") or "",
                "name": a.get("name"),
                "cwd": a.get("foreground_cwd") or a.get("cwd"),
                "branch": git_branch(a.get("foreground_cwd") or a.get("cwd")),
                "focused": a.get("focused", False),
                "cols": rect.get("width"), "rows": rect.get("height"),
                "waiting": waiting,
                **self.claude_info(a.get("agent"), (a.get("agent_session") or {}).get("value")),
            })
        # Plain terminals: panes with no agent in them.
        agent_panes = {a["pane_id"] for a in snap["agents"]}
        for pid, p in panes.items():
            if pid in agent_panes:
                continue
            prev = self.since.get(pid)
            if not prev or prev[0] != "shell":
                self.since[pid] = ("shell", now)
            rect, cwd = rects.get(pid) or {}, p.get("foreground_cwd") or p.get("cwd")
            cards.append({
                "pane": pid, "agent": None, "status": "shell", "herdrStatus": None, "since": self.since[pid][1],
                "workspace": ws.get(p["workspace_id"], {}).get("label") or p["workspace_id"], "workspaceId": p["workspace_id"],
                "tab": tabs.get(p["tab_id"], {}).get("number"), "label": p.get("label"),
                "title": p.get("terminal_title_stripped") or p.get("terminal_title") or "", "name": None,
                "cwd": cwd, "branch": git_branch(cwd), "focused": p.get("focused", False),
                "cols": rect.get("width"), "rows": rect.get("height"), "waiting": [],
            })
        cards += self.outside_cards({(a.get("agent_session") or {}).get("value") for a in snap["agents"]}, now)
        for gone in set(self.since) - {c["pane"] for c in cards}:
            self.since.pop(gone, None)
        self.pane_ids = set(panes)
        self.sessions = {c["session"] for c in cards if c.get("session")}
        # Rate limits: the newest statusline snapshot of each account.
        limits = {}
        for c in cards:
            h, k = c.get("hud") or {}, c.get("account")
            if k and (h.get("rate5h") or h.get("rate7d")) and h.get("at", 0) > limits.get(k, {}).get("at", -1):
                limits[k] = {"at": h["at"], "rate5h": h.get("rate5h"), "rate7d": h.get("rate7d")}
        return {
            "session": ARGS.session or "default",
            "readOnly": ARGS.read_only,
            "workspaces": [{"id": w["workspace_id"], "label": w["label"], "number": w["number"]} for w in snap["workspaces"]],
            "cards": cards,
            "limits": limits,
        }

    @staticmethod
    def claude_info(agent, sid):
        if agent != "claude" or not sid:
            return {}
        reg = registry().get(sid) or {}
        return {"session": sid, "account": account_kind(reg.get("_dir")), "hud": hud_for(sid)}

    def outside_cards(self, in_herdr, now):
        """Claude Code sessions running outside herdr, from Claude's own session registry."""
        cards = []
        for sid, r in registry().items():
            if not sid or sid in in_herdr or not pid_alive(r.get("pid")):
                continue
            status = {"busy": "working", "idle": "idle", "shell": "waiting"}.get(r.get("status"), "unknown")
            waiting = []
            if status in ("idle", "waiting"):
                try:
                    waiting = waiting_for(sid)
                except Exception:
                    pass
                status = "waiting" if waiting else "idle"
            cwd = r.get("cwd")
            cards.append({
                "pane": f"cc-{sid}", "external": True, "session": sid,
                "agent": "claude", "status": status, "herdrStatus": None,
                "since": r.get("statusUpdatedAt") or now,
                "workspace": r.get("name") or Path(cwd or "/").name,
                "workspaceId": "", "tab": None, "label": None,
                "title": f"outside herdr · {Path(r['_dir']).name}",
                "name": r.get("name"), "cwd": cwd, "branch": git_branch(cwd),
                "focused": False, "cols": None, "rows": None, "waiting": waiting,
                **self.claude_info("claude", sid),
            })
        return cards

    def refresh(self):
        try:
            data = self.build()
            self.error = None
        except Exception as e:
            data = {"error": f"herdr: {e}"}
        out = json.dumps(data, sort_keys=True)
        with self.cond:
            if out != self.json:
                self.json, self.version = out, self.version + 1
                self.cond.notify_all()

    def loop(self):
        while True:
            self.dirty.wait(timeout=3)  # events wake us early; 3 s catches what events miss (transcripts, TTL)
            self.dirty.clear()
            self.refresh()
            time.sleep(0.15)  # coalesce bursts of events


BOARD = Board()

GLOBAL_EVENTS = ["workspace.created", "workspace.updated", "workspace.renamed", "workspace.closed", "workspace.focused",
                 "workspace.moved", "workspace.reordered", "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
                 "pane.created", "pane.closed", "pane.updated", "pane.focused", "pane.moved", "pane.exited",
                 "pane.agent_detected", "layout.updated"]


def event_loop():
    """Holds one subscription; reconnects (and re-subscribes per-pane status) when the pane set changes."""
    while True:
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(SOCK)
                s.settimeout(2)
                known = set(BOARD.pane_ids)
                subs = [{"type": t} for t in GLOBAL_EVENTS] + [{"type": "pane.agent_status_changed", "pane_id": p} for p in known]
                s.sendall((json.dumps({"id": "sub", "method": "events.subscribe", "params": {"subscriptions": subs}}) + "\n").encode())
                while BOARD.pane_ids == known:
                    try:
                        if not s.recv(65536):
                            break
                        BOARD.dirty.set()
                    except socket.timeout:
                        pass
        except Exception:
            time.sleep(2)
        BOARD.dirty.set()


# ---------- HTTP ----------

def read_screen(pane, source="visible", lines=None):
    params = {"pane_id": pane, "source": source, "format": "ansi", "strip_ansi": False}
    if lines:
        params["lines"] = lines
    return call("pane.read", params)["read"]["text"]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg):
        self.send(code, json.dumps({"error": msg}))

    def sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def sse(self, data, event=None):
        msg = (f"event: {event}\n" if event else "") + "".join(f"data: {l}\n" for l in data.split("\n")) + "\n"
        self.wfile.write(msg.encode())
        self.wfile.flush()

    def known_pane(self, p):
        if not PANE_RE.match(p or "") or p not in BOARD.pane_ids:
            self.fail(400, "unknown pane")
            return False
        return True

    def do_GET(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:  # DNS-rebinding guard
            return self.fail(403, "bad host")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/events":
                self.sse_start()
                seen = -1
                while True:
                    with BOARD.cond:
                        BOARD.cond.wait_for(lambda: BOARD.version != seen, timeout=15)
                        v, data = BOARD.version, BOARD.json
                    if v == seen:
                        self.wfile.write(b": ping\n\n"); self.wfile.flush()
                    else:
                        seen = v
                        self.sse(data)
            if u.path == "/screen":
                pane = q.get("pane")
                if not self.known_pane(pane):
                    return
                self.sse_start()
                last, idle = None, 0
                while pane in BOARD.pane_ids:
                    try:
                        text = read_screen(pane)
                    except HerdrError as e:
                        self.sse(json.dumps({"error": str(e)})); break
                    if text != last:
                        last, idle = text, 0
                        self.sse(json.dumps({"text": text}))
                    else:
                        idle += 1
                        if idle % 100 == 0:
                            self.wfile.write(b": ping\n\n"); self.wfile.flush()
                    time.sleep(0.12)
                return
            if u.path == "/api/transcript":
                sid = q.get("session", "")
                if sid not in registry():
                    return self.fail(400, "unknown session")
                return self.send(200, json.dumps({"text": transcript_tail(sid)}))
            if u.path == "/api/chat":
                sid = q.get("session", "")
                if sid not in BOARD.sessions:
                    return self.fail(400, "unknown session")
                path = find_transcript(sid)
                if not path:
                    return self.send(200, json.dumps({"items": [], "mtime": 0}))
                mtime = str(os.path.getmtime(path))
                if q.get("since") == mtime:
                    return self.send(200, json.dumps({"same": True, "mtime": mtime}))
                return self.send(200, json.dumps({"items": chat_items(path), "mtime": mtime}))
            if u.path == "/api/meta":
                return self.send(200, json.dumps({
                    "accounts": [{k: a[k] for k in ("id", "email", "kind")} for a in ACCOUNTS],
                    "kinds": AGENT_KINDS, "folders": folders(), "home": HOME, "workDir": WORK_DIR,
                    "defaultAccount": {f: default_account(f) for f in folders() + [HOME]},
                }))
            if u.path == "/api/history":
                if self.known_pane(q.get("pane")):
                    self.send(200, json.dumps({"text": read_screen(q["pane"], "recent", 3000)}))
                return
            self.fail(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.fail(502, str(e))
            except Exception:
                pass

    def do_POST(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self.fail(403, "bad host")
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:  # CSRF guard
            return self.fail(403, "bad origin")
        if self.headers.get("Content-Type") != "application/json":
            return self.fail(415, "json only")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 256 * 1024:
            return self.fail(413, "too big")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            pane = body.get("pane")
            path = urlparse(self.path).path
            if ARGS.read_only and path in ("/api/new", "/api/input", "/api/prompt"):
                return self.fail(403, "server is read-only")
            if path == "/api/new":
                try:
                    out = create(body)
                except ValueError as e:
                    return self.fail(400, str(e))
                BOARD.dirty.set()
                return self.send(200, json.dumps(out))
            if not self.known_pane(pane):
                return
            if path == "/api/focus":
                call("pane.focus", {"pane_id": pane})
            elif path == "/api/prompt":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
                    return self.fail(400, "empty prompt")
                call("agent.prompt", {"target": pane, "text": text})
            elif path == "/api/input":
                params = {"pane_id": pane}
                if isinstance(body.get("text"), str) and body["text"]:
                    params["text"] = body["text"]
                keys = body.get("keys") or []
                if not isinstance(keys, list) or len(keys) > 64 or not all(isinstance(k, str) and KEY_RE.match(k) for k in keys):
                    return self.fail(400, "bad keys")
                if keys:
                    params["keys"] = keys
                call("pane.send_input", params)
            elif path == "/api/dismiss":
                BOARD.dismissed[pane] = next((a.get("completion_seq") for a in call("agent.list")["agents"] if a["pane_id"] == pane), None)
            else:
                return self.fail(404, "not found")
            self.send(200, '{"ok":true}')
            BOARD.dirty.set()
        except HerdrError as e:
            self.fail(400, str(e))
        except Exception as e:
            self.fail(500, str(e))

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


def replace_previous():
    """Stop the kanban this file started last time, so a herdr restart leaves exactly one running."""
    try:
        old = int(PIDFILE.read_text())
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(old)], capture_output=True, text=True).stdout
        if old != os.getpid() and "server.py" in cmd:
            os.kill(old, 15)
            for _ in range(50):
                if not pid_alive(old):
                    break
                time.sleep(0.1)
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    default_sock = os.path.expanduser("~/.config/herdr/herdr.sock")
    if ARGS.replace and ARGS.session is None and os.path.realpath(SOCK) != os.path.realpath(default_sock):
        raise SystemExit(0)  # plugin startup in a side session: keep the board on the default session
    if ARGS.replace:
        replace_previous()
    for attempt in range(30):  # the replaced server may take a moment to let go of the port
        try:
            srv = Server(("127.0.0.1", ARGS.port), H)
            break
        except OSError:
            if not ARGS.replace or attempt == 29:
                raise
            time.sleep(0.2)
    if ARGS.replace:
        PIDFILE.write_text(str(os.getpid()))
    BOARD.refresh()
    threading.Thread(target=BOARD.loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()
    print(f"herdr kanban on http://127.0.0.1:{ARGS.port}  (session: {ARGS.session or 'default'}, socket: {SOCK}"
          f"{', read-only' if ARGS.read_only else ''})", flush=True)
    srv.serve_forever()
