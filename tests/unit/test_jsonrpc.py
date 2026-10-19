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
"""Unit tests for the JSON-RPC connection.

A fake transport stands in for the WebSocket, so these run without
``websocket-client``; the real reader thread runs against it. The error
payloads mirror what TrueNAS sends.
"""

from __future__ import annotations

import errno
import inspect
import json
import queue
import socket
import ssl
import threading
import unittest
from collections.abc import Callable
from typing import Any, cast
from unittest import mock

from truenas_cinder import jsonrpc
from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASConnectionError,
    TrueNASNotFound,
    TrueNASTimeout,
)

JsonDict = dict[str, Any]
Handler = Callable[[JsonDict], 'list[object]']


class _FakeTransport:
    """In-memory WebSocket. ``handler`` maps each request to the messages
    TrueNAS sends back (dicts are JSON-encoded; '' closes; an exception is
    raised by ``recv``)."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler
        self.sent: list[JsonDict] = []
        self.inbox: queue.Queue[object] = queue.Queue()
        self.closed = False
        self.send_error: Exception | None = None
        self.certificate: bytes | None = None

    def deliver(self, message: object) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        self.inbox.put(message)

    def send(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        request = cast('JsonDict', json.loads(text))
        self.sent.append(request)
        if self.handler is not None:
            for message in self.handler(request):
                self.deliver(message)

    def recv(self) -> str:
        item = self.inbox.get(timeout=10)
        if isinstance(item, BaseException):
            raise item
        return cast('str', item)

    def close(self) -> None:
        self.closed = True
        self.inbox.put('')  # unblocks the reader

    def peer_certificate(self) -> bytes | None:
        return self.certificate


def _result(request: JsonDict, result: object) -> JsonDict:
    return {'jsonrpc': '2.0', 'id': request['id'], 'result': result}


def _error(request: JsonDict, error: object) -> JsonDict:
    return {'jsonrpc': '2.0', 'id': request['id'], 'error': error}


def _answering(result: object) -> Handler:
    return lambda request: [_result(request, result)]


def _connection(
    transport: _FakeTransport, call_timeout: float = 5.0
) -> jsonrpc.Connection:
    conn = jsonrpc.Connection(transport, call_timeout=call_timeout)
    return conn


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until ``condition`` holds; fail the test after ``timeout``."""
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        deadline.wait(0.01)
    raise AssertionError('timed out waiting for the fake transport')


def _in_thread(
    fn: Callable[[], object],
) -> tuple[threading.Thread, list[object]]:
    """Run ``fn`` in a thread; its result or exception lands in the list."""
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(fn())
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


class CallTest(unittest.TestCase):
    def test_request_shape_and_result(self) -> None:
        transport = _FakeTransport(_answering([{'id': 'tank'}]))
        conn = _connection(transport)
        self.assertEqual(
            conn.call('pool.query', [['name', '=', 'tank']], {}),
            [{'id': 'tank'}],
        )
        conn.call('core.ping')
        self.assertEqual(
            transport.sent,
            [
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'pool.query',
                    'params': [[['name', '=', 'tank']], {}],
                },
                {
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'core.ping',
                    'params': [],
                },
            ],
        )
        conn.close()

    def test_replies_are_matched_by_id(self) -> None:
        # Two calls in flight; TrueNAS answers the second one first.
        held: list[JsonDict] = []

        def handler(request: JsonDict) -> list[object]:
            if request['method'] == 'slow':
                held.append(request)
                return []
            return [
                _result(request, 'fast result'),
                _result(held[0], 'slow result'),
            ]

        transport = _FakeTransport(handler)
        conn = _connection(transport)
        thread, outcome = _in_thread(lambda: conn.call('slow'))
        _wait_for(lambda: bool(held))
        self.assertEqual(conn.call('fast'), 'fast result')
        thread.join(5)
        self.assertEqual(outcome, ['slow result'])
        conn.close()

    def test_notifications_are_ignored(self) -> None:
        def handler(request: JsonDict) -> list[object]:
            return [
                {
                    'jsonrpc': '2.0',
                    'method': 'collection_update',
                    'params': {'msg': 'changed', 'collection': 'x'},
                },
                'not json',
                _result(request, 'pong'),
            ]

        conn = _connection(_FakeTransport(handler))
        self.assertEqual(conn.call('core.ping'), 'pong')
        conn.close()

    def test_error_reply_raises(self) -> None:
        def handler(request: JsonDict) -> list[object]:
            return [
                _error(
                    request,
                    {
                        'code': jsonrpc.CALL_ERROR,
                        'message': 'Method call error',
                        'data': {
                            'error': errno.ENOENT,
                            'errname': 'ENOENT',
                            'reason': '[ENOENT] tank/x not found',
                        },
                    },
                )
            ]

        conn = _connection(_FakeTransport(handler))
        with self.assertRaises(TrueNASNotFound) as ctx:
            conn.call('pool.dataset.get_instance', 'tank/x')
        self.assertEqual(
            str(ctx.exception),
            'pool.dataset.get_instance failed: [ENOENT] tank/x not found',
        )
        conn.close()

    def test_error_without_an_id_fails_the_calls_in_flight(self) -> None:
        unanswered: list[JsonDict] = []
        transport = _FakeTransport(lambda r: unanswered.append(r) or [])
        conn = _connection(transport)
        thread, outcome = _in_thread(lambda: conn.call('core.ping'))
        _wait_for(lambda: bool(unanswered))
        transport.deliver(
            {
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32700, 'message': 'Parse error'},
            }
        )
        thread.join(5)
        self.assertIsInstance(outcome[0], TrueNASApiError)
        self.assertIn('Parse error', str(outcome[0]))
        conn.close()


class FailureTest(unittest.TestCase):
    def test_timeout(self) -> None:
        unanswered: list[JsonDict] = []
        transport = _FakeTransport(lambda r: unanswered.append(r) or [])
        conn = _connection(transport, call_timeout=0.05)
        with self.assertRaises(TrueNASTimeout):
            conn.call('pool.dataset.create', {'name': 'tank/x'})
        # The connection survives, and a late reply is dropped.
        transport.deliver(_result(unanswered[0], 'late'))
        transport.handler = _answering('pong')
        self.assertEqual(conn.call('core.ping'), 'pong')
        conn.close()

    def test_connection_lost_while_waiting(self) -> None:
        unanswered: list[JsonDict] = []
        transport = _FakeTransport(lambda r: unanswered.append(r) or [])
        conn = _connection(transport)
        thread, outcome = _in_thread(
            lambda: conn.call('pool.dataset.create', {'name': 'tank/x'})
        )
        _wait_for(lambda: bool(unanswered))
        transport.deliver(OSError('connection reset'))
        thread.join(5)
        failure = outcome[0]
        assert isinstance(failure, TrueNASConnectionError)
        # It was sent, so TrueNAS may have run it.
        self.assertTrue(failure.request_sent)
        self.assertIn('connection reset', str(failure))
        self.assertTrue(conn.closed)
        self.assertTrue(transport.closed)

    def test_server_close_refuses_further_calls(self) -> None:
        transport = _FakeTransport(_answering('pong'))
        conn = _connection(transport)
        transport.deliver('')
        _wait_for(lambda: conn.closed)
        with self.assertRaises(TrueNASConnectionError) as ctx:
            conn.call('core.ping')
        self.assertFalse(ctx.exception.request_sent)
        self.assertIn('closed', str(ctx.exception))

    def test_send_failure_is_safe_to_retry(self) -> None:
        transport = _FakeTransport()
        transport.send_error = OSError('broken pipe')
        conn = _connection(transport)
        with self.assertRaises(TrueNASConnectionError) as ctx:
            conn.call('pool.dataset.create', {'name': 'tank/x'})
        self.assertFalse(ctx.exception.request_sent)
        self.assertTrue(conn.closed)

    def test_close_is_idempotent(self) -> None:
        transport = _FakeTransport()
        conn = _connection(transport)
        conn.close()
        conn.close()
        self.assertTrue(conn.closed)
        self.assertTrue(transport.closed)

    def test_peer_certificate_comes_from_the_transport(self) -> None:
        transport = _FakeTransport()
        transport.certificate = b'der'
        conn = _connection(transport)
        self.assertEqual(conn.peer_certificate(), b'der')
        conn.close()


class ApiErrorTest(unittest.TestCase):
    """Decoding TrueNAS's JSON-RPC error objects."""

    def test_call_error_keeps_errno_and_reason(self) -> None:
        # Shape from a real TrueNAS reply.
        exc = jsonrpc.api_error(
            'core.ping',
            {
                'code': -32001,
                'message': 'Method call error',
                'data': {
                    'error': 207,
                    'errname': 'ENOTAUTHENTICATED',
                    'reason': '[ENOTAUTHENTICATED] Authentication required',
                    'trace': None,
                    'extra': [],
                },
            },
        )
        self.assertEqual(exc.errno, jsonrpc.ENOTAUTHENTICATED)
        self.assertNotIsInstance(exc, TrueNASNotFound)
        self.assertEqual(
            str(exc),
            'core.ping failed: [ENOTAUTHENTICATED] Authentication required',
        )

    def _validation(self, *codes: int) -> TrueNASApiError:
        return jsonrpc.api_error(
            'iscsi.target.delete',
            {
                'code': jsonrpc.INVALID_PARAMS,
                'message': 'Invalid params',
                'data': {
                    'error': errno.EINVAL,
                    'extra': [
                        [f'field{i}', f'problem {i}', code]
                        for i, code in enumerate(codes)
                    ],
                },
            },
        )

    def test_validation_error_all_enoent_is_not_found(self) -> None:
        # How TrueNAS says an object was already removed.
        exc = self._validation(errno.ENOENT)
        self.assertIsInstance(exc, TrueNASNotFound)
        self.assertEqual(
            str(exc), 'iscsi.target.delete failed: [ENOENT] field0: problem 0'
        )

    def test_mixed_validation_errors_are_not_not_found(self) -> None:
        # A partly invalid request must not read as "already gone".
        exc = self._validation(errno.ENOENT, errno.EINVAL)
        self.assertNotIsInstance(exc, TrueNASNotFound)
        self.assertIsNone(exc.errno)
        self.assertIn('[EINVAL] field1: problem 1', str(exc))

    def test_method_not_found(self) -> None:
        exc = jsonrpc.api_error(
            'auth.mechanism_choices',
            {'code': jsonrpc.METHOD_NOT_FOUND, 'message': 'Method not found'},
        )
        self.assertEqual(exc.errno, jsonrpc.ENOMETHOD)
        self.assertIn('Method not found', str(exc))

    def test_other_codes_use_the_message(self) -> None:
        exc = jsonrpc.api_error(
            'x', {'code': -32000, 'message': 'Too many concurrent calls'}
        )
        self.assertIsNone(exc.errno)
        self.assertEqual(str(exc), 'x failed: Too many concurrent calls')

    def test_malformed_errors(self) -> None:
        self.assertEqual(
            str(jsonrpc.api_error('x', 'boom')), "x failed: 'boom'"
        )
        self.assertEqual(
            str(jsonrpc.api_error('x', {'code': -1})),
            'x failed: JSON-RPC error -1',
        )

    def test_errname(self) -> None:
        self.assertEqual(jsonrpc.errname(errno.ENOENT), 'ENOENT')
        self.assertEqual(jsonrpc.errname(207), 'ENOTAUTHENTICATED')
        self.assertEqual(jsonrpc.errname(99999), '99999')


class OpenWebSocketTest(unittest.TestCase):
    def _open(
        self, verify_ssl: bool
    ) -> tuple[jsonrpc.Transport, mock.Mock, mock.Mock]:
        ws = mock.Mock()
        ws.sock = None
        module = mock.Mock()
        module.create_connection.return_value = ws
        with mock.patch.object(jsonrpc, '_websocket', module):
            transport = jsonrpc.open_websocket(
                'wss://h/api/current', verify_ssl, 10.0
            )
        return transport, module.create_connection, ws

    def test_verifies_certificates_by_default(self) -> None:
        _, create, ws = self._open(True)
        kwargs = create.call_args.kwargs
        self.assertEqual(create.call_args.args, ('wss://h/api/current',))
        self.assertEqual(kwargs['timeout'], 10.0)
        self.assertEqual(kwargs['sslopt'], {'cert_reqs': ssl.CERT_REQUIRED})
        self.assertIn(
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), kwargs['sockopt']
        )
        self.assertTrue(kwargs['enable_multithread'])
        # Calls time out on their own; the reader must block.
        ws.settimeout.assert_called_once_with(None)

    def test_skips_verification_when_asked(self) -> None:
        _, create, _ = self._open(False)
        self.assertEqual(
            create.call_args.kwargs['sslopt'],
            {'cert_reqs': ssl.CERT_NONE, 'check_hostname': False},
        )

    def test_connection_failure(self) -> None:
        module = mock.Mock()
        module.create_connection.side_effect = OSError('No route to host')
        with mock.patch.object(jsonrpc, '_websocket', module):
            with self.assertRaises(TrueNASConnectionError) as ctx:
                jsonrpc.open_websocket('wss://h/api/current', True, 10.0)
        self.assertIn('No route to host', str(ctx.exception))

    def test_missing_websocket_client(self) -> None:
        with (
            mock.patch.object(jsonrpc, '_websocket', None),
            mock.patch.object(
                jsonrpc, '_import_error', "No module named 'websocket'"
            ),
        ):
            with self.assertRaises(TrueNASConnectionError) as ctx:
                jsonrpc.open_websocket('wss://h/api/current', True, 10.0)
        self.assertIn('websocket-client', str(ctx.exception))
        self.assertIn("No module named 'websocket'", str(ctx.exception))

    def test_transport_decodes_bytes(self) -> None:
        transport, _, ws = self._open(True)
        ws.recv.return_value = b'{"id": 1}'
        self.assertEqual(transport.recv(), '{"id": 1}')

    def test_peer_certificate(self) -> None:
        transport, _, ws = self._open(True)
        self.assertIsNone(transport.peer_certificate())
        ws.sock = mock.Mock(spec=ssl.SSLSocket)
        ws.sock.getpeercert.return_value = b'der'
        self.assertEqual(transport.peer_certificate(), b'der')
        ws.sock.getpeercert.assert_called_with(binary_form=True)


@unittest.skipUnless(
    jsonrpc.DEPENDENCY_AVAILABLE, 'websocket-client is not installed'
)
class InstalledWebSocketContractTest(unittest.TestCase):
    """The installed websocket-client still has what the transport uses."""

    def test_connection_options_are_accepted(self) -> None:
        websocket = cast('Any', jsonrpc._websocket)
        # create_connection passes these through to WebSocket().
        inspect.signature(websocket.WebSocket).bind(
            sslopt={'cert_reqs': ssl.CERT_NONE},
            sockopt=[(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
            enable_multithread=True,
        )
        self.assertTrue(callable(websocket.create_connection))

    def test_websocket_has_the_used_members(self) -> None:
        websocket = cast('Any', jsonrpc._websocket)
        ws = websocket.WebSocket()
        self.assertIsNone(ws.sock)
        for name in ('send', 'recv', 'settimeout', 'close'):
            self.assertTrue(callable(getattr(ws, name)), name)


if __name__ == '__main__':
    unittest.main()
