"""Self-check for the Waiting-column transcript scanner. Run: python3 -I test_scanner.py"""
import importlib.util, json, os, sys, tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("server", os.path.join(HERE, "server.py"))
server = importlib.util.module_from_spec(spec)
with mock.patch.object(sys, "argv", ["server.py"]), mock.patch("subprocess.run") as run:
    run.return_value.stdout = json.dumps({"sessions": [{"name": "default", "default": True, "running": True, "socket_path": "/dev/null"}]})
    spec.loader.exec_module(server)

j = lambda o: json.dumps(o, separators=(",", ":"))
lines = [
    j({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"description": "Review diff"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"run_in_background": True, "description": "Run tests"}},
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"run_in_background": True, "description": "Server"}},
        {"type": "tool_use", "id": "t5", "name": "Agent", "input": {"description": "Lint"}}]}}),
    j({"type": "user", "timestamp": "2026-11-10T21:00:00Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "Async agent launched successfully. agentId: a1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "Command running in background with ID: b2"},
        {"type": "tool_result", "tool_use_id": "t3", "content": "Command running in background with ID: b3"},
        {"type": "tool_result", "tool_use_id": "t5", "content": "Async agent launched successfully. agentId: a5"}]}}),
    # b2 finished, a5 finished, b3 stopped with TaskStop, a1 only sent an interim report
    '{"type":"queue-operation","timestamp":"2026-11-10T21:01:00Z","content":"<task-notification><task-id>b2</task-id>completed</task-notification>"}',
    '{"type":"queue-operation","timestamp":"2026-11-10T21:01:00Z","content":"<task-notification><task-id>a5</task-id>done</task-notification>"}',
    '{"type":"queue-operation","timestamp":"2026-11-10T21:02:00Z","content":"<task-notification><task-id>a1</task-id>is waiting on its own background work</task-notification>"}',
    j({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t4", "name": "TaskStop", "input": {"task_id": "b3"}}]}}),
]
with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
    f.write("\n".join(lines))
try:
    got = server.scan_transcript(f.name)
finally:
    os.unlink(f.name)
assert [(i["id"], i["description"]) for i in got] == [("a1", "Review diff")], got
assert got[0]["since"] == server._iso_ms("2026-11-10T21:02:00Z"), got
print("ok")
