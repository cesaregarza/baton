"""What the event-stream client sends and records, judged from outside the client.

The client's own switches cannot be trusted to remove its tools, so the worker checks what it does instead.
Before each launch, the same launch runs against a `LoopbackStub`, a listener on 127.0.0.1 inside the worker
that captures requests and never answers with model output, and `request_problems` judges each captured
request. After each call, `record_problems` judges the client's session record, because the event stream does
not show every tool call, and `record_gaps` asks whether the record shows this call's finished turn at all.
Neither check runs a model.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import struct
import threading
from dataclasses import dataclass
from typing import Any

# The largest request or WebSocket message the stub reads; a launch check request is far smaller.
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
# Top-level fields of a plain turn request. Any other field is refused, so new context cannot slip in unseen.
REQUEST_FIELDS = frozenset(
    {
        "type",
        "model",
        "stream",
        "input",
        "tool_choice",
        "parallel_tool_calls",
        "store",
        "include",
        "prompt_cache_key",
        "text",
        "reasoning",
        "client_metadata",
        "generate",
        "service_tier",
    }
)
# Session record entries: what the client logs about a turn. Only the item entries can hold model actions.
RECORD_ENTRIES = frozenset(
    {"session_meta", "event_msg", "world_state", "turn_context", "response_item", "token_usage_record"}
)
RECORD_ITEMS = frozenset({"message", "reasoning"})  # response items
RECORD_EVENT_ITEMS = frozenset({"UserMessage", "AgentMessage", "Reasoning"})  # items inside item events
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def request_problems(request: Any, instructions: str, content: str, *, prewarm: bool) -> list[str]:
    """Why a captured request is not a plain turn carrying only Baton's text; empty when it is one.

    A plain turn offers no tools anywhere and holds only message items and reasoning-configuration items.
    Every developer message is exactly the instructions, which the client reads trimmed from their file, and
    the content appears exactly once as the user message; a prewarm request carries no user message.
    """
    if not isinstance(request, dict):
        return ["a request that is not a JSON object"]
    problems = [f"the request field {key}" for key in sorted(set(request) - REQUEST_FIELDS)]
    if _names_key(request, "tools"):
        problems.append("a tools field")
    items = request.get("input")
    if not isinstance(items, list):
        return [*problems, "no input list"]
    developer = user = 0
    for index, item in enumerate(items):
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "configuration_update" and set(item) <= {"type", "reasoning"}:
            continue
        if kind != "message":
            problems.append(f"input item {index} of type {kind}")
            continue
        role, texts = item.get("role"), _input_texts(item)
        if role == "developer" and texts == [instructions.strip()]:
            developer += 1
        elif role == "user" and texts == [content]:
            user += 1
        else:
            problems.append(f"input item {index}, a {role} message with other text")
    if developer == 0:
        problems.append("no developer message holding the instructions")
    if user != (0 if prewarm else 1):
        problems.append(f"the content {user} times")
    return problems


def record_problems(text: str) -> list[str]:
    """Why a session record shows more than messages and reasoning; empty when it shows nothing else."""
    problems = []
    for number, kind, payload in _entries(text):
        if kind is None:
            problems.append(f"an unreadable entry at line {number}")
        elif kind not in RECORD_ENTRIES:
            problems.append(f"an entry of type {kind} at line {number}")
        elif kind == "response_item" and payload.get("type") not in RECORD_ITEMS:
            problems.append(f"a {payload.get('type')} item at line {number}")
        elif kind == "event_msg" and "item" in payload:
            item = payload["item"]
            item_kind = item.get("type") if isinstance(item, dict) else None
            if item_kind not in RECORD_EVENT_ITEMS:
                problems.append(f"a {item_kind} item event at line {number}")
    return problems


def record_gaps(text: str, thread_id: str, content: str) -> list[str]:
    """What a session record lacks to show a finished turn of this call; empty when it shows one.

    The record must name the thread the event stream started and no other, hold this call's content as its one
    user message, and show an answer and the turn's completion. An empty or metadata-only record shows none of
    that, so the absence of a record of the turn is never read as a clean turn.
    """
    entries = [(kind, payload) for _, kind, payload in _entries(text)]
    messages = [payload for kind, payload in entries if kind == "response_item" and payload.get("type") == "message"]
    gaps = []
    if {payload.get("id") for kind, payload in entries if kind == "session_meta"} != {thread_id}:
        gaps.append("a session entry for this thread alone")
    if [_input_texts(message) for message in messages if message.get("role") == "user"] != [[content]]:
        gaps.append("this call's content as its one user message")
    if not any(message.get("role") == "assistant" for message in messages):
        gaps.append("an answer")
    if not any(kind == "event_msg" and payload.get("type") == "task_complete" for kind, payload in entries):
        gaps.append("the turn's completion")
    return gaps


def _entries(text: str) -> list[tuple[int, Any, dict[str, Any]]]:
    """Each nonblank line of a record: its number, entry type and payload. An unreadable line has no type."""
    entries = []
    for number, line in enumerate((line for line in text.split("\n") if line.strip()), 1):
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        payload = entry.get("payload") if isinstance(entry, dict) else None
        kind = entry.get("type") if isinstance(entry, dict) else None
        entries.append((number, kind, payload if isinstance(payload, dict) else {}))
    return entries


def _names_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_names_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_names_key(child, key) for child in value)
    return False


def _input_texts(item: dict[str, Any]) -> list[Any] | None:
    """The texts of a message's parts, or None when a part is anything but input text."""
    parts = item.get("content")
    if not isinstance(parts, list) or not all(isinstance(p, dict) and p.get("type") == "input_text" for p in parts):
        return None
    return [part.get("text") for part in parts]


@dataclass(frozen=True)
class Captured:
    """One request the stub read: its JSON body, whether it was a prewarm, and what made it unreadable if so."""

    body: Any
    prewarm: bool = False
    problem: str | None = None


class LoopbackStub:
    """A capture listener on 127.0.0.1 for the launch check: HTTP and WebSocket, never a model answer.

    `captured` is set once a turn request or an unacceptable request has arrived, which is the signal to stop
    the client. A request that carries an Authorization header, a compressed body or no JSON is recorded with
    its problem, never read further. A prewarm request gets an empty completed response so the client goes on
    to its turn request; every other request is answered with an error. Proxy tunnels are refused.
    """

    def __init__(self) -> None:
        self.captured = threading.Event()
        self.requests: list[Captured] = []
        self._lock = threading.Lock()
        handler = type("Handler", (_CaptureHandler,), {"stub": self})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="graph-launch-check", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> LoopbackStub:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def problems(self, instructions: str, content: str) -> list[str]:
        """Every reason to refuse the launch the captured requests show, or a missing turn request."""
        with self._lock:
            requests = list(self.requests)
        problems = [request.problem for request in requests if request.problem is not None]
        for request in requests:
            if request.problem is None:
                problems += request_problems(request.body, instructions, content, prewarm=request.prewarm)
        if not any(request.problem is None and not request.prewarm for request in requests):
            problems.append("no turn request")
        return problems

    def record(self, body: bytes | None, problem: str | None = None) -> bool:
        """Keep one captured request; True when it was a prewarm the client should be answered for."""
        parsed, prewarm = None, False
        if problem is None:
            try:
                parsed = json.loads(body or b"")
            except ValueError:
                problem = "a request body that is not JSON"
            else:
                prewarm = isinstance(parsed, dict) and parsed.get("generate") is False
        with self._lock:
            self.requests.append(Captured(parsed, prewarm, problem))
        if problem is not None or not prewarm:
            self.captured.set()
        return prewarm


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """One connection to the capture: what it reads goes to `stub`, and it answers only with errors."""

    stub: LoopbackStub  # set on the subclass each stub makes
    protocol_version = "HTTP/1.1"
    timeout = 10

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if not self.path.endswith("/responses"):
            self._answer(404)
            return
        problem = self._header_problem()
        if length > MAX_CAPTURE_BYTES:
            problem = problem or "a request over the capture bound"
        body = self.rfile.read(length) if problem is None else None
        self.stub.record(body, problem)
        self._answer(400)

    def do_GET(self) -> None:
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not self.path.endswith("/responses"):
            self._answer(404)
            return
        problem = self._header_problem()
        if problem is not None:
            self.stub.record(None, problem)
            self._answer(400)
            return
        self._websocket()

    def do_CONNECT(self) -> None:  # a proxy tunnel to anywhere else
        self._answer(405)

    def _header_problem(self) -> str | None:
        if "Authorization" in self.headers:
            return "a request carrying a credential"
        if (self.headers.get("Content-Encoding") or "identity").lower() != "identity":
            return "a compressed request body"
        return None

    def _answer(self, code: int) -> None:
        payload = b'{"error":{"message":"launch check","type":"invalid_request_error"}}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _websocket(self) -> None:
        key = (self.headers.get("Sec-WebSocket-Key") or "").encode()
        accept = base64.b64encode(hashlib.sha1(key + WEBSOCKET_GUID.encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)  # no extension is accepted, so frames are plain
        self.end_headers()
        self.wfile.flush()
        try:
            while (message := self._message()) is not None:
                if not self.stub.record(message):
                    break
                self._prewarm_reply()
        except (OSError, ValueError):
            self.stub.record(None, "an unreadable WebSocket message")
        self._send(0x8, struct.pack(">H", 1008))
        self.close_connection = True

    def _message(self) -> bytes | None:
        """The next whole text message, answering pings; None once the client closes."""
        parts, size = [], 0
        while True:
            head = self.rfile.read(2)
            if len(head) < 2:
                return None
            final, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if length >= 126:
                width = 2 if length == 126 else 8
                length = int.from_bytes(self.rfile.read(width), "big")
            size += length
            if size > MAX_CAPTURE_BYTES:
                raise ValueError("WebSocket message over the capture bound")
            mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(self.rfile.read(length)))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send(0xA, data)
            elif opcode in (0x0, 0x1, 0x2):
                parts.append(data)
                if final:
                    return b"".join(parts)

    def _prewarm_reply(self) -> None:
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        response = {"id": "resp_launch_check", "status": "completed", "output": [], "usage": usage}
        self._send(
            0x1,
            json.dumps({"type": "response.created", "response": {**response, "status": "in_progress"}}).encode(),
        )
        self._send(0x1, json.dumps({"type": "response.completed", "response": response}).encode())

    def _send(self, opcode: int, data: bytes) -> None:
        size = len(data)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([size])
        elif size < 1 << 16:
            head += bytes([126]) + struct.pack(">H", size)
        else:
            head += bytes([127]) + struct.pack(">Q", size)
        self.wfile.write(head + data)
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # nothing a client sent reaches the log
        pass
