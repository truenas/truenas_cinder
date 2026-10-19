# Copyright (c) 2026 TrueNAS
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""JSON-RPC 2.0 connection to the TrueNAS API over a WebSocket.

One connection carries every call. A reader thread matches each reply to its
call by id, so calls from several threads can be in flight at once. Messages
without an id are server notifications, which are ignored: the driver
subscribes to nothing.

This module neither logs in nor reconnects. A lost connection fails the
calls in flight and refuses new ones; ``client.TrueNASClient`` decides when
to open a new one.

``websocket-client`` is imported lazily: it is not a dependency of cinder
itself, so when it is missing the driver still loads and reports why from
``check_for_setup_error``.
"""

from __future__ import annotations

import errno as _errno
import itertools
import json
import socket
import ssl
import threading
from collections.abc import Callable
from typing import Any, Protocol, cast

from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASConnectionError,
    TrueNASNotFound,
    TrueNASTimeout,
)

_import_error: str | None = None
try:
    import websocket as _websocket
except ImportError as _exc:  # pragma: no cover - see check_for_setup_error
    _websocket = None
    _import_error = str(_exc)

#: True when ``websocket-client`` is importable in this environment.
DEPENDENCY_AVAILABLE: bool = _websocket is not None

#: Type alias for a decoded TrueNAS JSON object.
JsonDict = dict[str, Any]

# JSON-RPC error codes TrueNAS sends.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602  # validation errors, one entry per bad field
CALL_ERROR = -32001  # the method itself raised an error

# Error numbers TrueNAS defines beyond the operating system's.
ENOMETHOD = 201
ENOTAUTHENTICATED = 207
_TRUENAS_ERRNAMES = {
    201: 'ENOMETHOD',
    202: 'ESERVICESTARTFAILURE',
    203: 'EALERTCHECKERUNAVAILABLE',
    204: 'EREMOTENODEERROR',
    205: 'EDATASETISLOCKED',
    206: 'EINVALIDRRDTIMESTAMP',
    207: 'ENOTAUTHENTICATED',
    208: 'ESSLCERTVERIFICATIONERROR',
    209: 'EREBOOTREQUIRED',
    210: 'EHAUNAVAILABLE',
}

#: How long to wait for the TCP, TLS and WebSocket handshakes.
CONNECT_TIMEOUT = 10.0
#: How long to wait for the reply to a call.
CALL_TIMEOUT = 60.0


class Transport(Protocol):
    """A connected WebSocket, as the connection uses it."""

    def send(self, text: str) -> None: ...

    def recv(self) -> str:
        """Next text message; an empty string once the peer has closed."""
        ...

    def close(self) -> None: ...

    def peer_certificate(self) -> bytes | None:
        """The server's TLS certificate (DER), or None without TLS."""
        ...


#: Opens a ``Transport``: ``(url, verify_ssl, connect_timeout)``.
TransportFactory = Callable[[str, bool, float], Transport]


class _WebSocket(Protocol):
    """The part of ``websocket.WebSocket`` the transport uses."""

    sock: socket.socket | None

    def send(self, payload: str) -> int: ...

    def recv(self) -> str | bytes: ...

    def settimeout(self, timeout: float | None) -> None: ...

    def close(self) -> None: ...


class _WebSocketTransport:
    """``Transport`` over a ``websocket-client`` connection."""

    def __init__(self, ws: _WebSocket) -> None:
        self._ws = ws

    def send(self, text: str) -> None:
        self._ws.send(text)

    def recv(self) -> str:
        data = self._ws.recv()
        return data.decode('utf-8') if isinstance(data, bytes) else data

    def close(self) -> None:
        self._ws.close()

    def peer_certificate(self) -> bytes | None:
        sock = self._ws.sock
        if isinstance(sock, ssl.SSLSocket):
            # Available even when the certificate was not verified.
            return sock.getpeercert(binary_form=True)
        return None


def _keepalive_options() -> list[tuple[int, int, int]]:
    """TCP keepalive, so a silently dead peer is noticed within ~1 min."""
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (
        ('TCP_KEEPIDLE', 30),
        ('TCP_KEEPINTVL', 10),
        ('TCP_KEEPCNT', 3),
    ):
        option = getattr(socket, name, None)
        if isinstance(option, int):
            options.append((socket.IPPROTO_TCP, option, value))
    return options


def open_websocket(
    url: str, verify_ssl: bool, connect_timeout: float
) -> Transport:
    """Open a WebSocket to ``url`` with ``websocket-client``."""
    if _websocket is None:
        raise TrueNASConnectionError(
            f'websocket-client could not be imported ({_import_error}).'
        )
    sslopt: JsonDict = {'cert_reqs': ssl.CERT_REQUIRED}
    if not verify_ssl:
        sslopt = {'cert_reqs': ssl.CERT_NONE, 'check_hostname': False}
    create = cast('Callable[..., _WebSocket]', _websocket.create_connection)
    try:
        ws = create(
            url,
            timeout=connect_timeout,
            sslopt=sslopt,
            sockopt=_keepalive_options(),
            enable_multithread=True,
        )
    except Exception as exc:
        raise TrueNASConnectionError(
            f'Failed to connect to TrueNAS at {url}: {exc}'
        ) from exc
    # The reader blocks until a message arrives or the connection closes;
    # each call enforces its own timeout.
    ws.settimeout(None)
    return _WebSocketTransport(ws)


def errname(code: int) -> str:
    """Symbolic name of an error number, e.g. ``ENOENT``."""
    return _TRUENAS_ERRNAMES.get(code) or _errno.errorcode.get(code, str(code))


def _validation_entries(extra: object) -> list[tuple[str, str, int | None]]:
    """``(field, message, errno)`` for each entry of a validation error."""
    entries: list[tuple[str, str, int | None]] = []
    if not isinstance(extra, list):
        return entries
    for raw in cast('list[object]', extra):
        # JSON arrays decode as lists.
        if not isinstance(raw, list):
            continue
        item = cast('list[object]', raw)
        if len(item) >= 3:
            code = item[2]
            entries.append(
                (
                    str(item[0]),
                    str(item[1]),
                    code
                    if isinstance(code, int) and not isinstance(code, bool)
                    else None,
                )
            )
    return entries


def api_error(method: str, error: object) -> TrueNASApiError:
    """The exception for a JSON-RPC error object returned by ``method``.

    TrueNAS reports "does not exist" both as a call error with errno ENOENT
    and as a validation error whose entries all carry ENOENT; either becomes
    ``TrueNASNotFound``. A validation error mixing other codes keeps no
    errno, so a partly invalid request is never mistaken for "already gone".
    """
    if not isinstance(error, dict):
        return TrueNASApiError(f'{method} failed: {error!r}')
    err = cast('JsonDict', error)
    code = err.get('code')
    message = err.get('message')
    data_raw = err.get('data')
    data = cast('JsonDict', data_raw) if isinstance(data_raw, dict) else {}
    errno_val: int | None = None
    if code == INVALID_PARAMS:
        entries = _validation_entries(data.get('extra'))
        codes = {entry[2] for entry in entries}
        if len(codes) == 1:
            errno_val = codes.pop()
        detail = '; '.join(
            f'[{errname(entry[2]) if entry[2] is not None else "EINVAL"}] '
            f'{entry[0]}: {entry[1]}'
            for entry in entries
        )
    elif code == CALL_ERROR:
        raw_errno = data.get('error')
        if isinstance(raw_errno, int) and not isinstance(raw_errno, bool):
            errno_val = raw_errno
        reason = data.get('reason')
        detail = reason if isinstance(reason, str) else ''
    elif code == METHOD_NOT_FOUND:
        errno_val = ENOMETHOD
        detail = ''
    else:
        detail = ''
    if not detail:
        detail = (
            message
            if isinstance(message, str) and message
            else f'JSON-RPC error {code}'
        )
    if errno_val == _errno.ENOENT:
        return TrueNASNotFound(f'{method} failed: {detail}', errno=errno_val)
    return TrueNASApiError(f'{method} failed: {detail}', errno=errno_val)


class _Pending:
    """A call waiting for its reply."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: JsonDict | None = None
        # Why the connection closed while the call was waiting.
        self.lost: str | None = None


class Connection:
    """An open JSON-RPC connection to TrueNAS."""

    def __init__(
        self, transport: Transport, *, call_timeout: float = CALL_TIMEOUT
    ) -> None:
        self._transport = transport
        self._call_timeout = call_timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._closed_reason: str | None = None
        self._reader = threading.Thread(
            target=self._read_loop, name='truenas-jsonrpc', daemon=True
        )
        self._reader.start()

    @classmethod
    def open(
        cls,
        url: str,
        *,
        verify_ssl: bool = True,
        call_timeout: float = CALL_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        transport_factory: TransportFactory = open_websocket,
    ) -> Connection:
        transport = transport_factory(url, verify_ssl, connect_timeout)
        return cls(transport, call_timeout=call_timeout)

    @property
    def closed(self) -> bool:
        return self._closed_reason is not None

    def peer_certificate(self) -> bytes | None:
        """The server's TLS certificate (DER), or None without TLS."""
        return self._transport.peer_certificate()

    def call(self, method: str, *params: object) -> object:
        """Call ``method`` and return its result.

        Raises ``TrueNASApiError`` (or ``TrueNASNotFound``) for an error
        reply, ``TrueNASTimeout`` when no reply arrives in time, and
        ``TrueNASConnectionError`` when the connection is or becomes closed.
        """
        pending = _Pending()
        with self._lock:
            if self._closed_reason is not None:
                raise TrueNASConnectionError(
                    f'{method} failed: the connection to TrueNAS is closed '
                    f'({self._closed_reason}).'
                )
            call_id = next(self._ids)
            self._pending[call_id] = pending
        request = {
            'jsonrpc': '2.0',
            'id': call_id,
            'method': method,
            'params': list(params),
        }
        try:
            self._transport.send(json.dumps(request))
        except Exception as exc:
            with self._lock:
                self._pending.pop(call_id, None)
            self._shut_down(f'sending failed: {exc}')
            raise TrueNASConnectionError(
                f'{method} failed: could not send it to TrueNAS: {exc}'
            ) from exc
        if not pending.event.wait(self._call_timeout):
            with self._lock:
                self._pending.pop(call_id, None)
            raise TrueNASTimeout(
                f'{method} failed: TrueNAS did not answer within '
                f'{self._call_timeout:g}s.'
            )
        if pending.lost is not None:
            # The request was sent; TrueNAS may or may not have run it.
            raise TrueNASConnectionError(
                f'{method} failed: the connection to TrueNAS was lost '
                f'({pending.lost}).',
                request_sent=True,
            )
        reply = pending.reply or {}
        if 'error' in reply and reply['error'] is not None:
            raise api_error(method, reply['error'])
        return reply.get('result')

    def close(self) -> None:
        """Close the connection (idempotent)."""
        self._shut_down('closed by the driver')
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)

    # -- reader ------------------------------------------------------------

    def _read_loop(self) -> None:
        reason = 'TrueNAS closed the connection'
        try:
            while True:
                text = self._transport.recv()
                if not text:
                    break
                self._dispatch(text)
        except Exception as exc:
            reason = f'connection lost: {exc}'
        self._shut_down(reason)

    def _dispatch(self, text: str) -> None:
        try:
            decoded: object = json.loads(text)
        except ValueError:
            return
        if not isinstance(decoded, dict):
            return
        message = cast('JsonDict', decoded)
        call_id = message.get('id')
        if call_id is None:
            if message.get('error') is not None:
                # An error TrueNAS could not tie to a request (one it could
                # not parse): every call in flight may be the one.
                with self._lock:
                    waiting = list(self._pending.values())
                    self._pending.clear()
                for pending in waiting:
                    pending.reply = message
                    pending.event.set()
            return
        with self._lock:
            pending = self._pending.pop(call_id, None)
        if pending is None:
            # The call already timed out.
            return
        pending.reply = message
        pending.event.set()

    def _shut_down(self, reason: str) -> None:
        with self._lock:
            if self._closed_reason is not None:
                return
            self._closed_reason = reason
            waiting = list(self._pending.values())
            self._pending.clear()
        for pending in waiting:
            pending.lost = reason
            pending.event.set()
        try:
            self._transport.close()
        except Exception:  # pragma: no cover - best effort
            pass
