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
"""Unit tests for the TrueNAS client.

``tests.unit.fakes.FakeTrueNASClient`` replaces this whole class in the driver
tests, so these tests cover the layer the fake bypasses: the session
(connecting, reconnecting, which calls are retried), JSON-RPC parameter
shapes, and property parsing. The connection itself is faked here; its own
tests are in ``test_jsonrpc``. The response payloads below mirror what a
TrueNAS SCALE 25.10 system actually returns (plain integers from
``pool.query``, ``{'parsed': ...}`` property objects from ``pool.dataset.*``,
and an empty-string ``origin`` for a non-clone).
"""

from __future__ import annotations

import errno
import unittest
from typing import Any, cast
from unittest import mock

from truenas_cinder import client as tn_client
from truenas_cinder import jsonrpc
from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASConnectionError,
    TrueNASNotFound,
)


class _FakeConnection:
    """Stands in for ``jsonrpc.Connection``: records calls and answers them.

    ``replies`` maps a method to the answers for its successive calls (an
    exception is raised, anything else returned). Other calls return
    ``result``, except ``auth.login_ex``, which succeeds.
    """

    def __init__(
        self,
        result: Any = None,
        replies: dict[str, list[object]] | None = None,
    ) -> None:
        self.result: Any = result
        self.replies: dict[str, list[object]] = {
            method: list(answers)
            for method, answers in (replies or {}).items()
        }
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed: bool = False
        self.certificate: bytes | None = None

    def call(self, method: str, *params: object) -> object:
        self.calls.append((method, params))
        answers = self.replies.get(method)
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if method == 'auth.login_ex':
            return {'response_type': 'SUCCESS'}
        return self.result

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def peer_certificate(self) -> bytes | None:
        return self.certificate

    def close(self) -> None:
        self.closed = True


def _connected(
    result: Any = None,
) -> tuple[tn_client.TrueNASClient, _FakeConnection]:
    """A client wired to a recording connection, bypassing connect()."""
    client = tn_client.TrueNASClient()
    conn = _FakeConnection(result)
    client._conn = conn
    return client, conn


def _options(conn: _FakeConnection, index: int = 0) -> dict[str, Any]:
    """The single options object passed to a create-style call."""
    options = conn.calls[index][1][0]
    assert isinstance(options, dict)
    return cast('dict[str, Any]', options)


class _Factory:
    """Hands out the given connections in order, recording each open."""

    def __init__(self, *connections: _FakeConnection) -> None:
        self._connections = list(connections)
        self.opened: list[tuple[str, bool, float]] = []

    def __call__(
        self, url: str, verify_ssl: bool, call_timeout: float
    ) -> _FakeConnection:
        self.opened.append((url, verify_ssl, call_timeout))
        return self._connections.pop(0)


def _session(
    *connections: _FakeConnection,
) -> tuple[tn_client.TrueNASClient, _Factory]:
    """A client connected through the first of ``connections``."""
    factory = _Factory(*connections)
    client = tn_client.TrueNASClient(connection_factory=factory)
    client.connect('wss://h/api/current', 'truenas_admin', '7-secret')
    return client, factory


class ConnectTest(unittest.TestCase):
    def test_rejects_unknown_auth_mechanism(self) -> None:
        factory = mock.Mock()
        client = tn_client.TrueNASClient(connection_factory=factory)
        with self.assertRaises(TrueNASConnectionError) as ctx:
            client.connect(
                'wss://h/api/current', 'u', 'k', auth_mechanism='MAGIC'
            )
        self.assertIn('MAGIC', str(ctx.exception))
        # Rejected before any connection is attempted.
        factory.assert_not_called()

    def test_logs_in_with_the_configured_credentials(self) -> None:
        conn = _FakeConnection()
        _, factory = _session(conn)
        self.assertEqual(factory.opened, [('wss://h/api/current', True, 60.0)])
        self.assertEqual(
            conn.calls,
            [
                (
                    'auth.login_ex',
                    (
                        {
                            'mechanism': 'API_KEY_PLAIN',
                            'username': 'truenas_admin',
                            'api_key': '7-secret',
                        },
                    ),
                )
            ],
        )

    def test_rejected_login_closes_the_connection(self) -> None:
        conn = _FakeConnection(
            replies={'auth.login_ex': [{'response_type': 'AUTH_ERR'}]}
        )
        with self.assertRaises(TrueNASConnectionError) as ctx:
            _session(conn)
        self.assertIn('rejected the API key', str(ctx.exception))
        self.assertTrue(conn.closed)

    def test_call_without_connection_raises(self) -> None:
        client = tn_client.TrueNASClient()
        self.assertRaises(TrueNASConnectionError, client.ping)

    def test_close_is_idempotent(self) -> None:
        client, conn = _connected()
        client.close()
        self.assertTrue(conn.closed)
        client.close()

    def test_calls_after_close_do_not_reconnect(self) -> None:
        client, factory = _session(_FakeConnection(), _FakeConnection())
        client.close()
        self.assertRaises(TrueNASConnectionError, client.ping)
        self.assertEqual(len(factory.opened), 1)


class SessionTest(unittest.TestCase):
    """Reconnecting, and which calls may be sent again."""

    def test_reconnects_and_logs_in_after_the_connection_drops(self) -> None:
        first, second = _FakeConnection(), _FakeConnection(result='pong')
        client, factory = _session(first, second)
        first.closed = True  # e.g. TrueNAS restarted
        client.ping()
        self.assertEqual(len(factory.opened), 2)
        self.assertEqual(second.methods(), ['auth.login_ex', 'core.ping'])

    def test_call_that_never_left_is_sent_again(self) -> None:
        lost = TrueNASConnectionError('lost', request_sent=False)
        first = _FakeConnection(replies={'pool.dataset.create': [lost]})
        second = _FakeConnection(result={'id': 'tank/cinder/volume-1'})
        client, _ = _session(first, second)
        client.create_zvol(
            'tank/cinder/volume-1',
            1 << 30,
            volblocksize='16K',
            compression='lz4',
            sparse=True,
        )
        self.assertTrue(first.closed)
        self.assertEqual(
            second.methods(), ['auth.login_ex', 'pool.dataset.create']
        )

    def test_write_that_may_have_run_is_not_repeated(self) -> None:
        lost = TrueNASConnectionError('lost', request_sent=True)
        first = _FakeConnection(replies={'pool.dataset.create': [lost]})
        client, factory = _session(first, _FakeConnection())
        with self.assertRaises(TrueNASConnectionError):
            client.create_zvol(
                'tank/cinder/volume-1',
                1 << 30,
                volblocksize='16K',
                compression='lz4',
                sparse=True,
            )
        # Never sent twice: a second create could duplicate the volume.
        self.assertEqual(first.methods().count('pool.dataset.create'), 1)
        self.assertEqual(len(factory.opened), 1)

    def test_read_that_may_have_run_is_sent_again(self) -> None:
        lost = TrueNASConnectionError('lost', request_sent=True)
        first = _FakeConnection(replies={'pool.query': [lost]})
        second = _FakeConnection(result=[{'name': 'tank'}])
        client, _ = _session(first, second)
        self.assertEqual(client.get_pool('tank'), {'name': 'tank'})
        self.assertEqual(second.methods(), ['auth.login_ex', 'pool.query'])

    def test_a_second_failure_is_raised(self) -> None:
        lost = TrueNASConnectionError('lost', request_sent=False)
        first = _FakeConnection(replies={'core.ping': [lost]})
        second = _FakeConnection(replies={'core.ping': [lost]})
        client, _ = _session(first, second)
        self.assertRaises(TrueNASConnectionError, client.ping)

    def test_ended_session_logs_in_again_and_retries(self) -> None:
        ended = TrueNASApiError(
            'core.ping failed: not authenticated',
            errno=jsonrpc.ENOTAUTHENTICATED,
        )
        first = _FakeConnection(replies={'core.ping': [ended]})
        second = _FakeConnection(result='pong')
        client, factory = _session(first, second)
        client.ping()
        self.assertEqual(len(factory.opened), 2)
        self.assertEqual(second.methods(), ['auth.login_ex', 'core.ping'])

    def test_other_api_errors_are_not_retried(self) -> None:
        denied = TrueNASApiError('pool.query failed: no', errno=errno.EPERM)
        first = _FakeConnection(replies={'pool.query': [denied]})
        client, factory = _session(first)
        self.assertRaises(TrueNASApiError, client.get_pool, 'tank')
        self.assertEqual(len(factory.opened), 1)

    def test_a_failed_reconnect_is_raised(self) -> None:
        first = _FakeConnection()
        refused = _FakeConnection(
            replies={'auth.login_ex': [{'response_type': 'EXPIRED'}]}
        )
        client, _ = _session(first, refused)
        first.closed = True
        with self.assertRaises(TrueNASConnectionError) as ctx:
            client.ping()
        self.assertIn('expired', str(ctx.exception))


class DependencyTest(unittest.TestCase):
    def test_reports_the_websocket_import_error(self) -> None:
        # A present-but-broken install must not read as "not installed".
        with mock.patch.object(
            tn_client, '_import_error', "No module named 'websocket'"
        ):
            self.assertEqual(
                tn_client.TrueNASClient.dependency_error(),
                "No module named 'websocket'",
            )


class ApiVersionsTest(unittest.TestCase):
    def test_parses_version_list(self) -> None:
        payload = b'["v25.04.0", "v25.10.0"]'
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(
            tn_client.urllib.request, 'urlopen', return_value=response
        ):
            versions = tn_client.TrueNASClient.api_versions(
                'wss://host/api/current'
            )
        self.assertEqual(versions, ['v25.04.0', 'v25.10.0'])
        self.assertIn(tn_client.REQUIRED_API_VERSION, versions)

    def test_unreachable_endpoint_returns_empty(self) -> None:
        with mock.patch.object(
            tn_client.urllib.request,
            'urlopen',
            side_effect=OSError('unreachable'),
        ):
            self.assertEqual(
                tn_client.TrueNASClient.api_versions('wss://host/api/current'),
                [],
            )

    def test_non_list_payload_returns_empty(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"not": "a list"}'
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(
            tn_client.urllib.request, 'urlopen', return_value=response
        ):
            self.assertEqual(
                tn_client.TrueNASClient.api_versions('wss://host/api/current'),
                [],
            )


class NotFoundTest(unittest.TestCase):
    """How the typed methods treat "does not exist".

    The connection turns TrueNAS's ENOENT errors into ``TrueNASNotFound``
    (see ``test_jsonrpc``); here, deletes treat it as success.
    """

    def _client_raising(
        self, exc: BaseException
    ) -> tuple[tn_client.TrueNASClient, _FakeConnection]:
        client, conn = _connected()
        conn.call = mock.Mock(side_effect=exc)  # type: ignore[method-assign]
        return client, conn

    def test_deletes_treat_not_found_as_success(self) -> None:
        client, _ = self._client_raising(
            TrueNASNotFound('gone', errno=errno.ENOENT)
        )
        # Every delete is idempotent: "already gone" is success.
        client.delete_dataset('tank/cinder/volume-1')
        client.delete_snapshot('tank/cinder/volume-1@snap')
        client.delete_target(1)
        client.delete_extent(1)
        client.delete_targetextent(1)
        client.delete_auth(1)
        client.delete_initiator(1)

    def test_get_dataset_missing_returns_none(self) -> None:
        client, _ = self._client_raising(
            TrueNASNotFound('gone', errno=errno.ENOENT)
        )
        self.assertIsNone(client.get_dataset('tank/nope'))

    def test_other_errors_propagate(self) -> None:
        client, _ = self._client_raising(
            TrueNASApiError('denied', errno=errno.EPERM)
        )
        self.assertRaises(
            TrueNASApiError, client.delete_dataset, 'tank/cinder/volume-1'
        )


class CallShapeTest(unittest.TestCase):
    """Assert the exact JSON-RPC method names and parameter shapes."""

    def test_create_zvol_params(self) -> None:
        client, conn = _connected({'id': 'tank/cinder/volume-1'})
        client.create_zvol(
            'tank/cinder/volume-1',
            1073741824,
            volblocksize='16K',
            compression='lz4',
            sparse=True,
        )
        method, params = conn.calls[0]
        self.assertEqual(method, 'pool.dataset.create')
        options = params[0]
        assert isinstance(options, dict)
        self.assertEqual(options['type'], 'VOLUME')
        self.assertEqual(options['volsize'], 1073741824)
        self.assertEqual(options['volblocksize'], '16K')
        # Compression is upper-cased for the API.
        self.assertEqual(options['compression'], 'LZ4')
        self.assertTrue(options['sparse'])
        self.assertTrue(options['create_ancestors'])

    def test_create_zvol_thick_omits_sparse(self) -> None:
        client, conn = _connected({})
        client.create_zvol(
            'tank/v',
            1,
            volblocksize='16K',
            compression='LZ4',
            sparse=False,
        )
        options = _options(conn)
        self.assertNotIn('sparse', options)

    def test_promote_takes_single_positional_string(self) -> None:
        # Verified against the v25.10 API reference: pool.dataset.promote
        # takes one positional dataset id and returns null.
        client, conn = _connected(None)
        client.promote_dataset('tank/cinder/volume-clone')
        self.assertEqual(
            conn.calls[0],
            ('pool.dataset.promote', ('tank/cinder/volume-clone',)),
        )

    def test_rollback_params(self) -> None:
        client, conn = _connected(None)
        client.rollback_snapshot('tank/v@snap', force=True)
        method, params = conn.calls[0]
        self.assertEqual(method, 'pool.snapshot.rollback')
        self.assertEqual(params[0], 'tank/v@snap')
        self.assertEqual(params[1], {'force': True})

    def test_delete_snapshot_defer_flag(self) -> None:
        client, conn = _connected(None)
        client.delete_snapshot('tank/v@snap', defer=True)
        _, params = conn.calls[0]
        self.assertEqual(params[1], {'defer': True, 'recursive': False})

    def test_clone_snapshot_params(self) -> None:
        client, conn = _connected(None)
        client.clone_snapshot('tank/v@snap', 'tank/cinder/volume-2')
        method, params = conn.calls[0]
        self.assertEqual(method, 'pool.snapshot.clone')
        self.assertEqual(
            params[0],
            {'snapshot': 'tank/v@snap', 'dataset_dst': 'tank/cinder/volume-2'},
        )

    def test_target_delete_positional_flags(self) -> None:
        client, conn = _connected(None)
        client.delete_target(7, force=True, delete_extents=False)
        self.assertEqual(
            conn.calls[0], ('iscsi.target.delete', (7, True, False))
        )

    def test_extent_delete_positional_flags(self) -> None:
        client, conn = _connected(None)
        client.delete_extent(9, remove=False, force=True)
        self.assertEqual(
            conn.calls[0], ('iscsi.extent.delete', (9, False, True))
        )

    def test_create_targetextent_lun_zero(self) -> None:
        client, conn = _connected({'id': 1})
        client.create_targetextent(3, 4)
        self.assertEqual(
            conn.calls[0][1][0], {'target': 3, 'extent': 4, 'lunid': 0}
        )

    def test_pool_space_uses_options_object(self) -> None:
        # zfs.resource.query takes one options object, not [filters, opts].
        client, conn = _connected(
            [
                {
                    'name': 'tank',
                    'properties': {
                        'used': {
                            'value': 2319454208,
                            'raw': '2319454208',
                        },
                        'available': {
                            'value': 253633753088,
                            'raw': '253633753088',
                        },
                    },
                }
            ]
        )
        space = client.pool_space('tank')
        self.assertEqual(space.used, 2319454208)
        self.assertEqual(space.available, 253633753088)
        self.assertEqual(space.total, 2319454208 + 253633753088)
        method, params = conn.calls[0]
        self.assertEqual(method, 'zfs.resource.query')
        self.assertEqual(len(params), 1)
        self.assertEqual(
            params[0],
            {
                'paths': ['tank'],
                'properties': ['used', 'available'],
                'get_source': False,
            },
        )

    def test_pool_space_empty_result(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.pool_space('tank'), (0, 0))

    def test_pool_space_string_values(self) -> None:
        client, _ = _connected(
            [
                {
                    'properties': {
                        'used': {'value': '8'},
                        'available': {'value': '42'},
                    }
                }
            ]
        )
        self.assertEqual(client.pool_space('tank'), (8, 42))


class IscsiGlobalConfigTest(unittest.TestCase):
    def test_call_shape_and_result(self) -> None:
        # iscsi.global.config takes no parameters and is not a job.
        client, conn = _connected(
            {
                'id': 1,
                'basename': 'iqn.2000-01.io.truenas',
                'listen_port': 3260,
            }
        )
        config = client.iscsi_global_config()
        self.assertEqual(conn.calls[0], ('iscsi.global.config', ()))
        self.assertEqual(config['basename'], 'iqn.2000-01.io.truenas')
        self.assertEqual(config['listen_port'], 3260)


class PortalTest(unittest.TestCase):
    def test_wildcard_is_returned_verbatim_with_port(self) -> None:
        # TrueNAS commonly binds 0.0.0.0; the driver resolves the address.
        client, _ = _connected(
            [{'id': 1, 'listen': [{'ip': '0.0.0.0', 'port': 3260}]}]
        )
        self.assertEqual(
            client.portal_listen_addresses(1), [('0.0.0.0', 3260)]
        )

    def test_multiple_addresses_keep_their_own_ports(self) -> None:
        client, _ = _connected(
            [
                {
                    'id': 1,
                    'listen': [
                        {'ip': '10.0.0.1', 'port': 3260},
                        {'ip': '10.0.0.2', 'port': 3261},
                    ],
                }
            ]
        )
        self.assertEqual(
            client.portal_listen_addresses(1),
            [('10.0.0.1', 3260), ('10.0.0.2', 3261)],
        )

    def test_missing_port_is_none(self) -> None:
        client, _ = _connected([{'id': 1, 'listen': [{'ip': '10.0.0.1'}]}])
        self.assertEqual(
            client.portal_listen_addresses(1), [('10.0.0.1', None)]
        )

    def test_missing_portal(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.portal_listen_addresses(1), [])


class OriginParsingTest(unittest.TestCase):
    """Clone detection depends on parsing the ZFS ``origin`` property."""

    #: A non-clone reports origin as an empty string, not null.
    NON_CLONE: dict[str, Any] = {
        'id': 'tank/cinder/volume-a',
        'name': 'tank/cinder/volume-a',
        'origin': {
            'parsed': '',
            'rawvalue': '',
            'value': '',
            'source': 'NONE',
        },
    }
    CLONE: dict[str, Any] = {
        'id': 'tank/cinder/volume-b',
        'name': 'tank/cinder/volume-b',
        'origin': {
            'parsed': 'tank/cinder/volume-a@snap-1',
            'rawvalue': 'tank/cinder/volume-a@snap-1',
            'value': 'tank/cinder/volume-a@snap-1',
            'source': 'NONE',
        },
    }

    def test_clones_of_snapshot_matches_exact_origin(self) -> None:
        client, conn = _connected([self.NON_CLONE, self.CLONE])
        self.assertEqual(
            client.clones_of_snapshot('tank/cinder/volume-a@snap-1'),
            ['tank/cinder/volume-b'],
        )
        self.assertEqual(conn.calls[0][0], 'pool.dataset.query')

    def test_clones_of_snapshot_no_match(self) -> None:
        client, _ = _connected([self.NON_CLONE, self.CLONE])
        self.assertEqual(
            client.clones_of_snapshot('tank/cinder/volume-a@other'), []
        )

    def test_dependent_clones_matches_any_snapshot_of_dataset(self) -> None:
        client, _ = _connected([self.NON_CLONE, self.CLONE])
        self.assertEqual(
            client.dependent_clones('tank/cinder/volume-a'),
            ['tank/cinder/volume-b'],
        )

    def test_dependent_clones_ignores_unrelated_prefix(self) -> None:
        # 'volume-a2' must not match the 'volume-a@' prefix.
        other = dict(self.CLONE)
        other['origin'] = {'parsed': 'tank/cinder/volume-a2@snap'}
        client, _ = _connected([other])
        self.assertEqual(client.dependent_clones('tank/cinder/volume-a'), [])

    def test_plain_string_origin_is_supported(self) -> None:
        client, _ = _connected(
            [{'name': 'tank/c', 'origin': 'tank/cinder/volume-a@snap-1'}]
        )
        self.assertEqual(
            client.clones_of_snapshot('tank/cinder/volume-a@snap-1'),
            ['tank/c'],
        )


class AuthTagTest(unittest.TestCase):
    def test_next_auth_tag_on_empty_backend(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.next_auth_tag(), 1)

    def test_next_auth_tag_is_max_plus_one(self) -> None:
        client, _ = _connected([{'tag': 1}, {'tag': 7}, {'tag': 3}])
        self.assertEqual(client.next_auth_tag(), 8)

    def test_create_auth_omits_peer_when_not_mutual(self) -> None:
        client, conn = _connected({'id': 1, 'tag': 1})
        client.create_auth(1, 'user', 'secret123456')
        options = _options(conn)
        self.assertNotIn('peeruser', options)

    def test_create_auth_includes_peer_for_mutual(self) -> None:
        client, conn = _connected({'id': 1, 'tag': 1})
        client.create_auth(1, 'u', 's' * 12, peeruser='p', peersecret='q' * 12)
        options = _options(conn)
        self.assertEqual(options['peeruser'], 'p')


class PoolStatsTest(unittest.TestCase):
    def test_pool_query_returns_plain_integers(self) -> None:
        # pool.query returns size/allocated as plain ints (not property dicts).
        client, conn = _connected(
            [
                {
                    'id': 1,
                    'name': 'tank',
                    'healthy': True,
                    'status': 'ONLINE',
                    'size': 136902082560,
                    'allocated': 4292685824,
                    'free': 132609396736,
                }
            ]
        )
        pool = client.get_pool('tank')
        assert pool is not None
        self.assertEqual(pool['size'], 136902082560)
        self.assertEqual(conn.calls[0][0], 'pool.query')

    def test_get_pool_missing(self) -> None:
        client, _ = _connected([])
        self.assertIsNone(client.get_pool('nope'))

    def test_provisioned_bytes_sums_zvol_volsize(self) -> None:
        # Shape as pool.dataset.query returns it with these options: only
        # zvols carry volsize, as a property dict.
        gib = 1073741824
        client, conn = _connected(
            [
                {'id': 'tank', 'type': 'FILESYSTEM'},
                {'id': 'tank/cinder', 'type': 'FILESYSTEM'},
                {
                    'id': 'tank/cinder/volume-1',
                    'type': 'VOLUME',
                    'volsize': {
                        'parsed': gib,
                        'rawvalue': str(gib),
                        'value': '1 GiB',
                        'source': 'LOCAL',
                    },
                },
                {
                    'id': 'tank/vms/disk0',
                    'type': 'VOLUME',
                    'volsize': {'parsed': 3 * gib, 'rawvalue': str(3 * gib)},
                },
            ]
        )
        self.assertEqual(client.provisioned_bytes('tank'), 4 * gib)
        self.assertEqual(
            conn.calls[0],
            (
                'pool.dataset.query',
                (
                    [['pool', '=', 'tank']],
                    {'extra': {'flat': True, 'properties': ['volsize']}},
                ),
            ),
        )

    def test_provisioned_bytes_empty_pool(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.provisioned_bytes('tank'), 0)


if __name__ == '__main__':
    unittest.main()
