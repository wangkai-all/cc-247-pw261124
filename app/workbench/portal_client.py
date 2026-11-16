"""HTTP client used by the automated executor and by the workbench UI.

Every request identifies itself as the executor so the mock portal can inject
faults; the UI's "手工接管" flow uses the same client with ``source="human"``
but still carries the executor User-Agent, since only the outcome handling
differs, not the network path.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

EXECUTOR_UA = "collector-executor/1.0"
DEFAULT_TIMEOUT = 2.5


class PortalError(Exception):
    """Base class. ``outcome_unknown`` says whether a write may have landed."""

    outcome_unknown = True

    def __init__(self, message: str, kind: str = "error", status: int | None = None,
                 body: dict | None = None, outcome_unknown: bool | None = None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.body = body or {}
        if outcome_unknown is not None:
            self.outcome_unknown = outcome_unknown


class PortalTimeout(PortalError):
    outcome_unknown = True

    def __init__(self, message: str = "等待门户响应超时"):
        super().__init__(message, kind="timeout")


class PortalInterrupted(PortalError):
    """Connection closed before a complete response (lost receipt)."""

    outcome_unknown = True

    def __init__(self, message: str = "连接在收到完整回执前被断开"):
        super().__init__(message, kind="receipt_lost")


class PortalHTTP(PortalError):
    def __init__(self, status: int, body: dict, message: str = ""):
        kind = "http_%d" % status
        if status == 422:
            kind = "validation_failed"
        elif status == 409:
            kind = "version_conflict" if body.get("error") == "version_conflict" else "locked"
        elif status >= 500:
            kind = "server_error"
        super().__init__(message or body.get("message") or f"HTTP {status}", kind=kind, status=status, body=body)
        # Transient 5xx/lock may still be safe to retry; validation and
        # version conflicts are definite and must not be retried blindly.
        self.outcome_unknown = status >= 500 or kind == "locked"

class PortalUnavailable(PortalError):
    outcome_unknown = True

    def __init__(self, message: str = "无法连接门户"):
        super().__init__(message, kind="network")


def _json_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


class PortalClient:
    def __init__(
        self,
        base_url: str,
        fault_profile: str = "cm-default",
        timeout: float = DEFAULT_TIMEOUT,
        row_number: int | None = None,
        source: str = "collector",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fault_profile = fault_profile
        self.timeout = timeout
        self.row_number = row_number
        self.source = source

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": EXECUTOR_UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Fault-Profile": self.fault_profile,
        }
        if self.row_number is not None:
            headers["X-Row-Number"] = str(self.row_number)
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return _json_body(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise PortalHTTP(exc.code, _json_body(raw), "") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise PortalTimeout() from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise PortalTimeout() from exc
            raise PortalUnavailable(f"无法连接门户：{reason}") from exc
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            raise PortalInterrupted() from exc
        except OSError as exc:
            raise PortalUnavailable(f"网络错误：{exc}") from exc

    # -- API -------------------------------------------------------------
    def get_record(self, record_key: str) -> dict:
        return self._request("GET", "/api/record/" + urllib.parse.quote(record_key))

    def open_form(self, record_key: str) -> dict:
        return self._request("POST", "/api/executor/open", {"record_key": record_key})

    def stage(self, session_token: str, fields: dict[str, str]) -> dict:
        return self._request(
            "POST", "/api/executor/stage", {"session_token": session_token, "fields": fields}
        )

    def submit(self, session_token: str) -> dict:
        return self._request("POST", "/api/executor/submit", {"session_token": session_token})

    def receipt(self, sub_token: str) -> dict:
        return self._request("GET", "/api/receipt/" + urllib.parse.quote(sub_token))
