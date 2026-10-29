"""Stdio smoke tests: spawn the plugin and play the host side, including
answering the reverse sampling/createMessage request."""

import json
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "my_first_anna_app_plugin.py"


class Host:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, str(PLUGIN)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", bufsize=1)

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def recv(self):
        line = self.p.stdout.readline()
        assert line, "plugin closed stdout"
        return json.loads(line)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)


def _init(h):
    h.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2.0", "capabilities": {"sampling": {}}}})
    r = h.recv()
    assert r["result"]["protocolVersion"] == "2.0"
    assert "sampling" in r["result"]["capabilities"]


def test_describe_and_ping():
    h = Host()
    try:
        _init(h)
        h.send({"jsonrpc": "2.0", "id": 1, "method": "describe"})
        m = h.recv()["result"]
        assert "llm.sample" in m["host_capabilities"]
        assert {t["name"] for t in m["tools"]} == {"chat", "summarize_conversation", "ping"}
        h.send({"jsonrpc": "2.0", "id": 2, "method": "invoke", "params": {"tool": "ping", "arguments": {}}})
        r = h.recv()["result"]
        assert r["success"] is True and r["data"]["pong"] is True and r["data"]["v2"] is True
    finally:
        h.close()


def test_chat_uses_reverse_sampling_with_vision_context():
    h = Host()
    try:
        _init(h)
        h.send({"jsonrpc": "2.0", "id": 5, "method": "invoke", "params": {
            "tool": "chat",
            "arguments": {"messages": [{"role": "user", "content": "What can I make?"}],
                          "vision_context": '{"kind": "ingredients", "items": ["eggs", "tomato"]}'},
            "context": {"invoke_id": "inv-1", "sampling_token": "tok"}}})
        req = h.recv()
        assert req["method"] == "sampling/createMessage"
        p = req["params"]
        assert p["includeContext"] == "none" and p["maxTokens"] <= 8192
        assert p["context"]["invoke_id"] == "inv-1"
        last = p["messages"][-1]["content"]
        assert last["type"] == "text" and "eggs" in last["text"] and "INGREDIENTS" in last["text"]
        h.send({"jsonrpc": "2.0", "id": req["id"], "result": {
            "role": "assistant", "content": {"type": "text", "text": "Make a shakshuka! 💡"}, "model": "m"}})
        r = h.recv()
        assert r["id"] == 5
        data = r["result"]["data"]
        assert data["reply"] == "Make a shakshuka! 💡" and data["mode"] == "ingredients" and data["model"] == "m"

        # Second sequential message in the same conversation & process (with emoji in history!)
        h.send({"jsonrpc": "2.0", "id": 6, "method": "invoke", "params": {
            "tool": "chat",
            "arguments": {"messages": [
                {"role": "user", "content": "What can I make?"},
                {"role": "assistant", "content": "Make a shakshuka! 💡"},
                {"role": "user", "content": "what about making cake?"},
            ]},
            "context": {"invoke_id": "inv-2", "sampling_token": "tok"}}})
        req2 = h.recv()
        assert req2["method"] == "sampling/createMessage"
        assert len(req2["params"]["messages"]) == 3
        h.send({"jsonrpc": "2.0", "id": req2["id"], "result": {
            "role": "assistant", "content": {"type": "text", "text": "Here is how to bake a cake 🎂!"}, "model": "m"}})
        r2 = h.recv()
        assert r2["id"] == 6
        assert "cake" in r2["result"]["data"]["reply"]
    finally:
        h.close()


def test_summarize_parses_json():
    h = Host()
    try:
        _init(h)
        h.send({"jsonrpc": "2.0", "id": 7, "method": "invoke", "params": {
            "tool": "summarize_conversation",
            "arguments": {"messages": [{"role": "user", "content": "How do I make jollof rice?"},
                                       {"role": "assistant", "content": "Here is how..."}]}}})
        req = h.recv()
        h.send({"jsonrpc": "2.0", "id": req["id"], "result": {"content": {"type": "text",
                "text": '{"title": "Jollof Rice Tutorial", "summary": "Steps for jollof."}'}}})
        r = h.recv()["result"]["data"]
        assert r == {"title": "Jollof Rice Tutorial", "summary": "Steps for jollof."}
    finally:
        h.close()


def test_sampling_error_is_propagated():
    h = Host()
    try:
        _init(h)
        h.send({"jsonrpc": "2.0", "id": 9, "method": "invoke", "params": {
            "tool": "chat", "arguments": {"messages": [{"role": "user", "content": "hi"}]}}})
        req = h.recv()
        h.send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32001, "message": "SAMPLING_NOT_GRANTED"}})
        r = h.recv()
        assert r["error"]["code"] == -32001
    finally:
        h.close()


def test_invalid_args():
    h = Host()
    try:
        _init(h)
        h.send({"jsonrpc": "2.0", "id": 3, "method": "invoke", "params": {"tool": "chat", "arguments": {"messages": []}}})
        assert h.recv()["error"]["code"] == -32602
    finally:
        h.close()

