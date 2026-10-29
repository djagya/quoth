"""Stdlib-only JSON client for the Quoth sidecar's Unix socket (no deps in the gateway)."""
from __future__ import annotations

import http.client
import json
import socket


class SidecarUnavailable(RuntimeError):
    """The sidecar socket is missing, refused the connection or timed out."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class SidecarClient:
    def __init__(self, socket_path: str, timeout: float = 5.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = None) -> dict:
        """Return the decoded JSON response ({"ok": bool, ...}); raise SidecarUnavailable."""
        conn = _UnixConnection(self.socket_path, timeout or self.timeout)
        try:
            payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, path, body=payload, headers=headers)
            raw = conn.getresponse().read()
        except (OSError, http.client.HTTPException) as exc:
            raise SidecarUnavailable(type(exc).__name__) from exc
        finally:
            conn.close()
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise SidecarUnavailable("invalid_response") from exc

    def get(self, path: str, timeout: float | None = None) -> dict:
        return self.call("GET", path, None, timeout)

    def post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        return self.call("POST", path, body, timeout)
