"""本地 LLM HTTP 替身（测试专用，不发任何外部请求）。

- LLMStandIn：进程内 ThreadingHTTPServer，路径第一段选择场景（/{scenario}/chat/completions），逐场景计数，
  并记录收到的 Authorization 头，用来证明密钥确实发到了替身、却没有出现在异常与日志里。
- SilentTLSServer：只接受 TCP、从不回应 TLS 握手，用于制造真实的「连接超时」（httpx 的 TLS 握手算连接阶段）。
- CountingTransport：包在真实 HTTPTransport 外面数尝试次数（连接被拒时替身收不到请求，只能在客户端侧计数）。
- closed_port()：拿一个刚释放、无人监听的本机端口，用于「连接被拒」。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


def completion(
    content: str = "替身回答",
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
    with_usage: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "cmpl-standin",
        "object": "chat.completion",
        "model": "standin-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
    }
    if with_usage:
        body["usage"] = usage or {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}
    return body


class _Handler(BaseHTTPRequestHandler):
    server: _StandInServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # 静默：替身日志不进 pytest 输出

    def _send(self, status: int, payload: Any, *, headers: dict[str, str] | None = None, raw: bytes | None = None,
              content_type: str = "application/json") -> None:
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-request-id", f"req-{self._scenario}-{self._n}")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        parts = self.path.strip("/").split("/")
        self._scenario = parts[0]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        with self.server.lock:
            self.server.counts[self._scenario] += 1
            self._n = self.server.counts[self._scenario]
            self.server.auth_seen.append(auth)
        getattr(self, f"_scenario_{self._scenario}")(auth)

    # ---------------- 场景 ----------------
    # 错误体故意回显 Authorization：真实服务商的 401 文案常带部分 key，客户端不得把响应体原样带进异常/日志

    def _scenario_ok(self, auth: str) -> None:
        self._send(200, completion())

    def _scenario_auth401(self, auth: str) -> None:
        self._send(401, {"error": {"message": f"Authentication Fails, invalid key {auth}", "type": "auth"}})

    def _scenario_auth403(self, auth: str) -> None:
        self._send(403, {"error": {"message": f"forbidden for {auth}"}})

    def _scenario_rate429(self, auth: str) -> None:
        self._send(429, {"error": {"message": f"rate limited {auth}"}}, headers={"Retry-After": "3"})

    def _scenario_rate429long(self, auth: str) -> None:
        self._send(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "3600"})

    def _scenario_server500(self, auth: str) -> None:
        self._send(500, {"error": {"message": f"internal error while handling {auth}"}})

    def _scenario_flaky503(self, auth: str) -> None:
        if self._n == 1:
            self._send(503, {"error": {"message": "overloaded"}})
        else:
            self._send(200, completion("重试后的回答"))

    def _scenario_readtimeout(self, auth: str) -> None:
        time.sleep(self.server.slow_seconds)
        with contextlib.suppress(OSError):  # 客户端已因读超时断开
            self._send(200, completion())

    def _scenario_interrupted(self, auth: str) -> None:
        # 声明 400 字节，只写一小段就断开：响应读到一半中断
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "400")
        self.send_header("x-request-id", f"req-interrupted-{self._n}")
        self.end_headers()
        self.wfile.write(b'{"id": "cmpl-standin", "choices": [')
        self.wfile.flush()
        self.close_connection = True
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _scenario_notjson(self, auth: str) -> None:
        self._send(200, None, raw=f"<html>502 gateway {auth}</html>".encode(), content_type="text/html")

    def _scenario_nochoices(self, auth: str) -> None:
        self._send(200, {"id": "cmpl-standin", "object": "chat.completion",
                         "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}})

    def _scenario_badtypes(self, auth: str) -> None:
        self._send(200, {"id": "cmpl-standin", "choices": "oops", "usage": {"prompt_tokens": "x"}})

    def _scenario_length(self, auth: str) -> None:
        self._send(200, completion("回答写到一半", finish_reason="length",
                                   usage={"prompt_tokens": 40, "completion_tokens": 16, "total_tokens": 56}))

    def _scenario_contentfilter(self, auth: str) -> None:
        self._send(200, completion("", finish_reason="content_filter"))

    def _scenario_nousage(self, auth: str) -> None:
        self._send(200, completion("没有 usage 的回答", with_usage=False))


class _StandInServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.counts: Counter[str] = Counter()
        self.auth_seen: list[str] = []
        self.slow_seconds = 1.5


class LLMStandIn:
    def __init__(self) -> None:
        self._server = _StandInServer()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> LLMStandIn:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def url(self, scenario: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/{scenario}"

    def count(self, scenario: str) -> int:
        with self._server.lock:
            return self._server.counts[scenario]

    @property
    def total(self) -> int:
        with self._server.lock:
            return sum(self._server.counts.values())

    @property
    def auth_seen(self) -> list[str]:
        with self._server.lock:
            return list(self._server.auth_seen)


class SilentTLSServer:
    """接受 TCP 连接但从不回应 TLS 握手：客户端在连接阶段超时。accepted 为接受的连接数。"""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self._conns: list[socket.socket] = []
        self._stop = threading.Event()
        self.accepted = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            self._conns.append(conn)  # 持有不读不写

    def __enter__(self) -> SilentTLSServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for c in self._conns:
            c.close()
        self._sock.close()

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self._sock.getsockname()[1]}"


class CountingTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return super().handle_request(request)


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
