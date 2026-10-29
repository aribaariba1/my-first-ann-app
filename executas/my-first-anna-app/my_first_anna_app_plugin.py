#!/usr/bin/env python3
"""Smart Kitchen Assistant - Python Executa (stdio JSON-RPC 2.0, protocol v2).

Flow:
    iframe --anna.tools.invoke--> this Executa --sampling/createMessage--> Anna LLM

The iframe does the *vision* step (anna.llm.complete with image blocks,
because Executa sampling is text-only) and hands the plugin a text
``vision_context``. This plugin owns the cooking "brain":

* ``chat``                    - kitchen-assistant reply for a conversation
* ``summarize_conversation``  - short title + summary for the history sidebar
* ``ping``                    - smoke test

Pure standard library (no executa-sdk dependency) so it runs under
``uv run`` and packages cleanly with PyInstaller. The reverse-RPC wire
format mirrors ``executa_sdk.SamplingClient`` from the official examples.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VERSION = "0.2.0"
PROTOCOL_VERSION_V2 = "2.0"
MAX_TOKENS_CAP = 8192          # host maxTokensPerCall
MAX_SAMPLING_MESSAGES = 64     # host per-request message cap
SAMPLING_TIMEOUT_S = 60.0      # keep inside tools.invoke 90s ceiling

MANIFEST = {
    "name": "tool-dev-my-first-anna-app",
    "display_name": "Smart Kitchen Assistant",
    "version": VERSION,
    "description": (
        "Cooking assistant brain for the Smart Kitchen Assistant app: suggests "
        "recipes from available ingredients, teaches how to cook a pictured "
        "dish, and summarizes conversations. Uses host sampling (no API key)."
    ),
    "author": "Smart Kitchen Assistant",
    # Required for reverse sampling; without it the host answers -32008.
    "host_capabilities": ["llm.sample"],
    "tools": [
        {
            "name": "chat",
            "description": (
                "Produce the kitchen assistant's next reply for a conversation. "
                "Optionally include a text description of photos/video frames "
                "(ingredients or a finished dish) produced by a vision model."
            ),
            "parameters": [
                {
                    "name": "messages",
                    "type": "array",
                    "items_type": "object",
                    "description": "Conversation so far: [{role:'user'|'assistant', content:string}]. Last item is the new user turn.",
                    "required": True,
                },
                {
                    "name": "vision_context",
                    "type": "string",
                    "description": "Optional analysis of the images attached to the latest user turn.",
                    "required": False,
                    "default": "",
                },
                {
                    "name": "mode",
                    "type": "string",
                    "description": "auto | ingredients | dish | general",
                    "required": False,
                    "default": "auto",
                },
            ],
        },
        {
            "name": "summarize_conversation",
            "description": "Return a short title and one-paragraph summary for a conversation.",
            "parameters": [
                {
                    "name": "messages",
                    "type": "array",
                    "items_type": "object",
                    "description": "Conversation: [{role, content}].",
                    "required": True,
                }
            ],
        },
        {
            "name": "ping",
            "description": "Smoke-test method.",
            "parameters": [],
        },
    ],
}

SYSTEM_PROMPT = """You are "Smart Kitchen Assistant", a warm, practical home-cooking expert.
Always answer in clear Markdown.

Modes:
- INGREDIENTS: the user shows/lists ingredients they have. Suggest 3-5 dishes they can make,
  most feasible first. For each: name, one-line description, which of their ingredients it uses,
  any small extras they may need, time and difficulty. Offer to give the full recipe for one.
- DISH: the user shows/names a finished meal. Identify the dish (say if you are unsure),
  then give: a full ingredient list with quantities (for ~2-4 servings), equipment,
  numbered step-by-step instructions, timing, and tips/variations.
- GENERAL: answer any cooking question (techniques, substitutions, timing, safety, meal plans).

Rules: be concise but complete; use metric and US units where helpful; flag food-safety
issues and common allergens; never invent that you saw something not described in the
image analysis; if the image analysis says it is not food, say so politely."""

_stdout_lock = threading.Lock()
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()
_sampling_disabled_reason: str | None = None
_negotiated_v2 = True


class SamplingError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}


# ---------------------------------------------------------------- frame I/O

def _write_frame(msg: dict) -> None:
    # Use ensure_ascii=True so stdout only ever emits 7-bit ASCII (\uXXXX
    # escapes for emojis/accents). On Windows, Python's default sys.stdout
    # encoding is cp1252 ('charmap'), which crashes with UnicodeEncodeError
    # when an LLM response or conversation history contains emojis like 💡.
    payload = json.dumps(msg, ensure_ascii=True)
    with _stdout_lock:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def _response(req_id, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


# ------------------------------------------------------------ reverse RPC

def sample(messages: list[dict], *, max_tokens: int, system_prompt: str | None,
           temperature: float | None, invoke_id: str, tool: str,
           response_format: dict | None = None) -> dict:
    """Blocking sampling/createMessage reverse RPC (one per call)."""
    if _sampling_disabled_reason:
        raise SamplingError(-32008, _sampling_disabled_reason)
    req_id = uuid.uuid4().hex
    params: dict = {
        "messages": messages,
        "maxTokens": max(16, min(MAX_TOKENS_CAP, int(max_tokens))),
        "includeContext": "none",
        "_clientTimeoutS": SAMPLING_TIMEOUT_S,
        "metadata": {"executa_invoke_id": invoke_id, "tool": tool},
    }
    if system_prompt:
        params["systemPrompt"] = system_prompt
    if temperature is not None:
        params["temperature"] = temperature
    if response_format:
        params["responseFormat"] = response_format
        params["onUnsupported"] = "text"
    if invoke_id:
        params["context"] = {"invoke_id": invoke_id}

    slot = {"event": threading.Event(), "msg": None}
    with _pending_lock:
        _pending[req_id] = slot
    _write_frame({"jsonrpc": "2.0", "id": req_id, "method": "sampling/createMessage", "params": params})
    if not slot["event"].wait(SAMPLING_TIMEOUT_S):
        with _pending_lock:
            _pending.pop(req_id, None)
        raise SamplingError(-32005, f"sampling/createMessage timed out after {SAMPLING_TIMEOUT_S}s")
    msg = slot["msg"] or {}
    if msg.get("error"):
        err = msg["error"]
        raise SamplingError(int(err.get("code", -32603)), str(err.get("message", "error")), err.get("data"))
    return msg.get("result") or {}


def _dispatch_response(msg: dict) -> bool:
    with _pending_lock:
        slot = _pending.pop(msg.get("id"), None)
    if slot is None:
        return False
    slot["msg"] = msg
    slot["event"].set()
    return True


def _text_of(result: dict) -> str:
    content = result.get("content") or {}
    if isinstance(content, dict):
        return content.get("text", "") or ""
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


# ------------------------------------------------------------------ tools

def _clean_messages(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("'messages' must be a non-empty array")
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = m.get("content")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            continue
        out.append({"role": role, "text": text.strip()})
    if not out:
        raise ValueError("'messages' contains no usable user/assistant text")
    return out


def detect_mode(text: str, vision_context: str) -> str:
    v = (vision_context or "").lower()
    if '"kind": "ingredients"' in v or "kind: ingredients" in v:
        return "ingredients"
    if '"kind": "dish"' in v or "kind: dish" in v:
        return "dish"
    t = (text or "").lower()
    if re.search(r"\b(i have|what can i (make|cook)|ingredients? (i have|available)|in my fridge|leftover)", t):
        return "ingredients"
    if re.search(r"\b(how (do|to) (i )?(make|cook)|recipe for|teach me)", t):
        return "dish"
    return "general"


def build_sampling_messages(msgs: list[dict], vision_context: str, mode: str) -> list[dict]:
    # Keep the newest turns within the host cap.
    msgs = msgs[-MAX_SAMPLING_MESSAGES:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    out = []
    for i, m in enumerate(msgs):
        text = m["text"]
        if i == len(msgs) - 1 and m["role"] == "user":
            extra = [f"[Assistant mode: {mode.upper()}]"]
            if vision_context:
                extra.append("[Image analysis of the photos the user attached]\n" + vision_context.strip())
            if len(msgs) > 1:
                extra.append("[Note: Continue the conversation naturally and answer the user's latest question directly and concisely.]")
            text = "\n\n".join(extra + ["[User message]\n" + text])
        out.append({"role": m["role"], "content": {"type": "text", "text": text}})
    return out


def tool_chat(args: dict, invoke_id: str) -> dict:
    msgs = _clean_messages(args.get("messages"))
    if msgs[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    vision = str(args.get("vision_context") or "")[:12000]
    mode = str(args.get("mode") or "auto").lower()
    if mode not in ("ingredients", "dish", "general"):
        # Only use vision_context to auto-switch mode on the turn that actually
        # attached the photo; follow-up text questions default to general unless
        # the user's new text asks for ingredients/dish.
        mode = detect_mode(msgs[-1]["text"], vision)
    result = sample(
        build_sampling_messages(msgs, vision, mode),
        max_tokens=800, system_prompt=SYSTEM_PROMPT, temperature=0.6,
        invoke_id=invoke_id, tool="chat",
    )
    reply = _text_of(result).strip()
    first_user = msgs[0]["text"].replace("\n", " ").strip()
    title = (first_user[:45] + "…") if len(first_user) > 45 else first_user
    plain_reply = re.sub(r"[#*_`>-]+", " ", reply)
    plain_reply = re.sub(r"\s+", " ", plain_reply).strip()
    summary = (plain_reply[:110] + "…") if len(plain_reply) > 110 else plain_reply
    return {"reply": reply, "mode": mode, "title": title, "summary": summary,
            "model": result.get("model"), "usage": result.get("usage")}


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def tool_summarize(args: dict, invoke_id: str) -> dict:
    msgs = _clean_messages(args.get("messages"))
    transcript = "\n".join(f"{m['role'].upper()}: {m['text'][:1500]}" for m in msgs[-20:])
    result = sample(
        [{"role": "user", "content": {"type": "text", "text":
            "Summarize this cooking conversation. Reply ONLY with JSON: "
            '{"title": "<max 6 words>", "summary": "<max 40 words>"}\n\n' + transcript}}],
        max_tokens=200, system_prompt="You write very short titles and summaries.",
        temperature=0.2, invoke_id=invoke_id, tool="summarize_conversation",
        response_format={"type": "json_object"},
    )
    data = _parse_json(_text_of(result))
    title = str(data.get("title") or msgs[0]["text"][:40]).strip().strip('"')[:60]
    return {"title": title or "Cooking chat", "summary": str(data.get("summary") or "")[:400]}


TOOLS = {
    "chat": tool_chat,
    "summarize_conversation": tool_summarize,
    "ping": lambda args, invoke_id: {"pong": True, "version": VERSION, "v2": _negotiated_v2},
}


# ---------------------------------------------------------------- handlers

def _handle_initialize(req_id, params: dict) -> dict:
    global _negotiated_v2, _sampling_disabled_reason
    proto = str((params or {}).get("protocolVersion") or "1.1")
    _negotiated_v2 = proto == PROTOCOL_VERSION_V2
    if not _negotiated_v2:
        _sampling_disabled_reason = (
            f"host did not negotiate v2 (offered protocolVersion={proto!r}); "
            "sampling/createMessage requires Executa protocol 2.0"
        )
    else:
        _sampling_disabled_reason = None
    return _response(req_id, result={
        "protocolVersion": proto if proto in ("1.1", "2.0") else PROTOCOL_VERSION_V2,
        "server_info": {"name": "smart-kitchen-assistant", "version": VERSION},
        "serverInfo": {"name": "smart-kitchen-assistant", "version": VERSION},
        "capabilities": {"sampling": {}} if _negotiated_v2 else {},
        "client_capabilities": {"sampling": {}} if _negotiated_v2 else {},
    })


def _handle_invoke(req_id, params: dict) -> dict:
    tool = params.get("tool")
    args = params.get("arguments") or {}
    ctx = params.get("context") or {}
    invoke_id = ctx.get("invoke_id") or params.get("invoke_id") or ""
    fn = TOOLS.get(tool)
    if fn is None:
        return _response(req_id, error={"code": -32601, "message": f"Unknown tool: {tool}"})
    try:
        data = fn(args, invoke_id)
    except SamplingError as e:
        return _response(req_id, error={"code": e.code, "message": e.message, "data": e.data})
    except (TypeError, ValueError) as e:
        return _response(req_id, error={"code": -32602, "message": f"Invalid params: {e}"})
    except Exception as e:  # noqa: BLE001
        return _response(req_id, error={"code": -32603, "message": f"Tool execution failed: {e}"})
    # Dispatcher contract envelope.
    return _response(req_id, result={"success": True, "tool": tool, "data": data})


def handle_message(line: str) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        _write_frame(_response(None, error={"code": -32700, "message": "Parse error"}))
        return
    if "method" not in msg:  # reply to one of our reverse RPCs
        if not _dispatch_response(msg):
            print(f"unmatched response id={msg.get('id')!r}", file=sys.stderr)
        return
    method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        resp = _handle_initialize(req_id, params)
    elif method == "describe":
        resp = _response(req_id, result=MANIFEST)
    elif method == "invoke":
        resp = _handle_invoke(req_id, params)
    elif method == "health":
        resp = _response(req_id, result={"status": "ready", "version": VERSION,
                                         "timestamp": datetime.now(timezone.utc).isoformat()})
    elif method == "shutdown":
        resp = _response(req_id, result={"ok": True})
    else:
        resp = _response(req_id, error={"code": -32601, "message": f"Method not found: {method}"})
    if req_id is not None:
        _write_frame(resp)


def main() -> None:
    with contextlib.suppress(Exception):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    print("smart-kitchen-assistant executa started", file=sys.stderr)
    # Thread pool: an invoke blocks while waiting for its sampling reply,
    # which arrives on this same stdin loop.
    # NOTE: use sys.stdin.readline() instead of `for raw in sys.stdin:`
    # because Python 3.10's file iterator uses an internal read-ahead buffer
    # on Windows pipes, blocking until EOF or buffer fill and hanging the
    # second JSON-RPC call!
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="invoke")
    try:
        while True:
            raw = sys.stdin.readline()
            if not raw:
                break
            line = raw.strip()
            if line:
                pool.submit(handle_message, line)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
