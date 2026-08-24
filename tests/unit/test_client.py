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
"""Unit tests for the TrueNAS client adapter.

``tests.unit.fakes.FakeTrueNASClient`` replaces this whole class in the driver
tests, so these tests cover the layer the fake bypasses: JSON-RPC parameter
shapes, error translation, and property parsing. The response payloads below
mirror what a TrueNAS SCALE 25.10 appliance actually returns (plain integers
from ``pool.query``, ``{'parsed': ...}`` property objects from
``pool.dataset.*``, and an empty-string ``origin`` for a non-clone).
"""

import errno
import unittest
from typing import Any, cast
from unittest import mock

from truenas_cinder import client as tn_client
from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASConnectionError,
    TrueNASNotFound,
)


class _RecordingRaw:
    """Minimal stand-in for ``truenas_api_client.Client``."""

    def __init__(self, result: Any = None) -> None:
        self.result: Any = result
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.raises: BaseException | None = None
        self.login_args: tuple[Any, ...] | None = None
        self.login_kwargs: dict[str, Any] | None = None
        self.closed: bool = False

    def call(self, method: str, *params: object) -> object:
        self.calls.append((method, params))
        if self.raises is not None:
            raise self.raises
        return self.result

    def login_with_api_key(self, *args: Any, **kwargs: Any) -> None:
        self.login_args = args
        self.login_kwargs = kwargs

    def close(self) -> None:
        self.closed = True


def _connected(
    result: Any = None,
) -> tuple[tn_client.TrueNASClient, _RecordingRaw]:
    """A client wired to a recording raw stub, bypassing connect()."""
    client = tn_client.TrueNASClient()
    raw = _RecordingRaw(result)
    client._client = raw  # type: ignore[assignment]
    return client, raw


def _options(raw: _RecordingRaw, index: int = 0) -> dict[str, Any]:
    """The single options object passed to a create-style call."""
    options = raw.calls[index][1][0]
    assert isinstance(options, dict)
    return cast('dict[str, Any]', options)


class ConnectTest(unittest.TestCase):
    def test_rejects_unknown_auth_mechanism(self) -> None:
        client = tn_client.TrueNASClient()
        with self.assertRaises(TrueNASConnectionError) as ctx:
            client.connect(
                'wss://h/api/current', 'u', 'k', auth_mechanism='MAGIC'
            )
        self.assertIn('MAGIC', str(ctx.exception))

    def test_passes_mechanism_and_channel_binding(self) -> None:
        client = tn_client.TrueNASClient()
        raw = _RecordingRaw()
        with mock.patch.object(tn_client, '_client_factory', return_value=raw):
            client.connect(
                'wss://h/api/current',
                'truenas_admin',
                'key',
                verify_ssl=False,
                auth_mechanism='plain',
            )
        assert raw.login_args is not None
        self.assertEqual(raw.login_args[0], 'truenas_admin')
        self.assertEqual(raw.login_args[1], 'key')
        # Mechanism is normalised to upper case.
        self.assertEqual(str(raw.login_args[2]), 'PLAIN')
        assert raw.login_kwargs is not None
        # channel_binding defaults to verify_ssl.
        self.assertFalse(raw.login_kwargs['channel_binding'])

    def test_call_without_connection_raises(self) -> None:
        client = tn_client.TrueNASClient()
        self.assertRaises(TrueNASConnectionError, client.ping)

    def test_close_is_idempotent(self) -> None:
        client, raw = _connected()
        client.close()
        self.assertTrue(raw.closed)
        client.close()


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


class ErrorTranslationTest(unittest.TestCase):
    def _raise(self, exc: BaseException) -> BaseException:
        client, raw = _connected()
        raw.raises = exc
        with self.assertRaises(Exception) as ctx:
            client.ping()
        return ctx.exception

    def test_enoent_maps_to_not_found(self) -> None:
        assert tn_client._ClientException is not None
        exc = tn_client._ClientException('missing', errno.ENOENT)
        result = self._raise(exc)
        self.assertIsInstance(result, TrueNASNotFound)

    def test_other_errno_maps_to_api_error(self) -> None:
        assert tn_client._ClientException is not None
        exc = tn_client._ClientException('boom', errno.EPERM)
        result = self._raise(exc)
        self.assertIsInstance(result, TrueNASApiError)
        self.assertNotIsInstance(result, TrueNASNotFound)
        self.assertEqual(getattr(result, 'errno', None), errno.EPERM)

    def test_not_found_is_swallowed_by_delete_paths(self) -> None:
        assert tn_client._ClientException is not None
        client, raw = _connected()
        raw.raises = tn_client._ClientException('gone', errno.ENOENT)
        # Every delete is idempotent: "already gone" is success.
        client.delete_dataset('tank/cinder/volume-1')
        client.delete_snapshot('tank/cinder/volume-1@snap')
        client.delete_target(1)
        client.delete_extent(1)
        client.delete_targetextent(1)
        client.delete_auth(1)
        client.delete_initiator(1)

    def _validation_errors(self, *codes: int) -> BaseException:
        """A ValidationErrors carrying the given per-entry error codes.

        TrueNAS reports "does not exist" this way, and the class sets no
        top-level errno -- the code lives on each collected entry.
        """
        from truenas_api_client.exc import ValidationErrors
        from truenas_api_client.jsonrpc import ErrorExtra

        return ValidationErrors(
            [
                ErrorExtra('ALL', f'object {i} does not exist', code)
                for i, code in enumerate(codes)
            ]
        )

    def test_validation_error_enoent_maps_to_not_found(self) -> None:
        exc = self._validation_errors(errno.ENOENT)
        self.assertIsNone(getattr(exc, 'errno', None))
        result = self._raise(exc)
        self.assertIsInstance(result, TrueNASNotFound)

    def test_validation_error_enoent_makes_deletes_idempotent(self) -> None:
        client, raw = _connected()
        raw.raises = self._validation_errors(errno.ENOENT)
        # This is the shape a real appliance returns for an already-removed
        # object, so every delete path must still treat it as success.
        client.delete_targetextent(1701)
        client.delete_target(1839)
        client.delete_extent(1634)
        client.delete_dataset('tank/cinder/volume-gone')
        self.assertIsNone(client.get_dataset('tank/cinder/volume-gone'))

    def test_mixed_validation_errors_are_not_not_found(self) -> None:
        # A partial failure must not be mistaken for "already gone".
        exc = self._validation_errors(errno.ENOENT, errno.EINVAL)
        result = self._raise(exc)
        self.assertIsInstance(result, TrueNASApiError)
        self.assertNotIsInstance(result, TrueNASNotFound)

    def test_get_dataset_missing_returns_none(self) -> None:
        assert tn_client._ClientException is not None
        client, raw = _connected()
        raw.raises = tn_client._ClientException('gone', errno.ENOENT)
        self.assertIsNone(client.get_dataset('tank/nope'))


class CallShapeTest(unittest.TestCase):
    """Assert the exact JSON-RPC method names and parameter shapes."""

    def test_create_zvol_params(self) -> None:
        client, raw = _connected({'id': 'tank/cinder/volume-1'})
        client.create_zvol(
            'tank/cinder/volume-1',
            1073741824,
            volblocksize='16K',
            compression='lz4',
            sparse=True,
        )
        method, params = raw.calls[0]
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
        client, raw = _connected({})
        client.create_zvol(
            'tank/v',
            1,
            volblocksize='16K',
            compression='LZ4',
            sparse=False,
        )
        options = _options(raw)
        self.assertNotIn('sparse', options)

    def test_promote_takes_single_positional_string(self) -> None:
        # Verified against the v25.10 API reference: pool.dataset.promote
        # takes one positional dataset id and returns null.
        client, raw = _connected(None)
        client.promote_dataset('tank/cinder/volume-clone')
        self.assertEqual(
            raw.calls[0],
            ('pool.dataset.promote', ('tank/cinder/volume-clone',)),
        )

    def test_rollback_params(self) -> None:
        client, raw = _connected(None)
        client.rollback_snapshot('tank/v@snap', force=True)
        method, params = raw.calls[0]
        self.assertEqual(method, 'pool.snapshot.rollback')
        self.assertEqual(params[0], 'tank/v@snap')
        self.assertEqual(params[1], {'force': True})

    def test_delete_snapshot_defer_flag(self) -> None:
        client, raw = _connected(None)
        client.delete_snapshot('tank/v@snap', defer=True)
        _, params = raw.calls[0]
        self.assertEqual(params[1], {'defer': True, 'recursive': False})

    def test_clone_snapshot_params(self) -> None:
        client, raw = _connected(None)
        client.clone_snapshot('tank/v@snap', 'tank/cinder/volume-2')
        method, params = raw.calls[0]
        self.assertEqual(method, 'pool.snapshot.clone')
        self.assertEqual(
            params[0],
            {'snapshot': 'tank/v@snap', 'dataset_dst': 'tank/cinder/volume-2'},
        )

    def test_target_delete_positional_flags(self) -> None:
        client, raw = _connected(None)
        client.delete_target(7, force=True, delete_extents=False)
        self.assertEqual(
            raw.calls[0], ('iscsi.target.delete', (7, True, False))
        )

    def test_extent_delete_positional_flags(self) -> None:
        client, raw = _connected(None)
        client.delete_extent(9, remove=False, force=True)
        self.assertEqual(
            raw.calls[0], ('iscsi.extent.delete', (9, False, True))
        )

    def test_create_targetextent_lun_zero(self) -> None:
        client, raw = _connected({'id': 1})
        client.create_targetextent(3, 4)
        self.assertEqual(
            raw.calls[0][1][0], {'target': 3, 'extent': 4, 'lunid': 0}
        )

    def test_available_bytes_uses_options_object(self) -> None:
        # zfs.resource.query takes one options object, not [filters, opts].
        client, raw = _connected(
            [
                {
                    'properties': {
                        'available': {
                            'value': 106551668736,
                            'raw': '106551668736',
                        }
                    }
                }
            ]
        )
        self.assertEqual(client.available_bytes('tank'), 106551668736)
        method, params = raw.calls[0]
        self.assertEqual(method, 'zfs.resource.query')
        self.assertEqual(len(params), 1)
        self.assertEqual(
            params[0],
            {
                'paths': ['tank'],
                'properties': ['available'],
                'get_source': False,
            },
        )

    def test_available_bytes_empty_result(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.available_bytes('tank'), 0)

    def test_available_bytes_string_value(self) -> None:
        client, _ = _connected(
            [{'properties': {'available': {'value': '42'}}}]
        )
        self.assertEqual(client.available_bytes('tank'), 42)


class PortalTest(unittest.TestCase):
    def test_listen_ips_wildcard_is_returned_verbatim(self) -> None:
        # A real appliance commonly binds 0.0.0.0; the driver resolves it.
        client, _ = _connected(
            [{'id': 1, 'listen': [{'ip': '0.0.0.0', 'port': 3260}]}]
        )
        self.assertEqual(client.portal_listen_ips(1), ['0.0.0.0'])

    def test_listen_ips_multiple(self) -> None:
        client, _ = _connected(
            [
                {
                    'id': 1,
                    'listen': [
                        {'ip': '10.0.0.1', 'port': 3260},
                        {'ip': '10.0.0.2', 'port': 3260},
                    ],
                }
            ]
        )
        self.assertEqual(client.portal_listen_ips(1), ['10.0.0.1', '10.0.0.2'])

    def test_listen_ips_missing_portal(self) -> None:
        client, _ = _connected([])
        self.assertEqual(client.portal_listen_ips(1), [])


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
        client, raw = _connected([self.NON_CLONE, self.CLONE])
        self.assertEqual(
            client.clones_of_snapshot('tank/cinder/volume-a@snap-1'),
            ['tank/cinder/volume-b'],
        )
        self.assertEqual(raw.calls[0][0], 'pool.dataset.query')

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
        client, raw = _connected({'id': 1, 'tag': 1})
        client.create_auth(1, 'user', 'secret123456')
        options = _options(raw)
        self.assertNotIn('peeruser', options)

    def test_create_auth_includes_peer_for_mutual(self) -> None:
        client, raw = _connected({'id': 1, 'tag': 1})
        client.create_auth(1, 'u', 's' * 12, peeruser='p', peersecret='q' * 12)
        options = _options(raw)
        self.assertEqual(options['peeruser'], 'p')


class PoolStatsTest(unittest.TestCase):
    def test_pool_query_returns_plain_integers(self) -> None:
        # pool.query returns size/allocated as plain ints (not property dicts).
        client, raw = _connected(
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
        self.assertEqual(raw.calls[0][0], 'pool.query')

    def test_get_pool_missing(self) -> None:
        client, _ = _connected([])
        self.assertIsNone(client.get_pool('nope'))


if __name__ == '__main__':
    unittest.main()
