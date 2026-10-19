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
"""Typed TrueNAS API client used by the driver.

Every TrueNAS call the driver makes goes through a typed method here, so the
rest of the codebase never touches raw dicts of ``Any``. Underneath,
``jsonrpc`` carries the calls and ``auth`` logs in. This class owns the
session: it reconnects and logs in again when the connection drops, and
decides which calls may be sent again.

All TrueNAS calls here are synchronous JSON-RPC 2.0 requests; none of the
dataset, snapshot, or iSCSI methods used by the driver are jobs.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, NamedTuple, Protocol, cast

from oslo_log import log as logging

from truenas_cinder import auth, common, jsonrpc
from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASConnectionError,
    TrueNASNotFound,
)

LOG = logging.getLogger(__name__)

#: API-key authentication mechanisms (see ``auth``).
AUTH_PLAIN: str = auth.AUTH_PLAIN
AUTH_SCRAM: str = auth.AUTH_SCRAM
AUTH_MECHANISMS: tuple[str, ...] = auth.AUTH_MECHANISMS

#: API version this driver is developed and tested against.
REQUIRED_API_VERSION: str = 'v25.10.0'

#: True when ``websocket-client``, the one third-party library the client
#: needs, is importable in this environment.
DEPENDENCY_AVAILABLE: bool = jsonrpc.DEPENDENCY_AVAILABLE
#: Why ``websocket-client`` could not be imported, if it could not.
_import_error: str | None = jsonrpc._import_error

#: Type alias for a decoded TrueNAS JSON object.
JsonDict = dict[str, Any]

# Calls that only read, so are safe to send again after a lost connection
# even though TrueNAS may already have run them.
_READ_ONLY_SUFFIXES = ('.query', '.get_instance', '.config')
_READ_ONLY_METHODS = frozenset({'core.ping', 'auth.mechanism_choices'})


def _is_read_only(method: str) -> bool:
    return method in _READ_ONLY_METHODS or method.endswith(_READ_ONLY_SUFFIXES)


class PoolSpace(NamedTuple):
    """Usable space on a pool, in bytes, from its root dataset."""

    used: int
    available: int

    @property
    def total(self) -> int:
        return self.used + self.available


class _Connection(Protocol):
    """What the client needs from a connection (``jsonrpc.Connection``)."""

    @property
    def closed(self) -> bool: ...

    def call(self, method: str, *params: object) -> object: ...

    def peer_certificate(self) -> bytes | None: ...

    def close(self) -> None: ...


#: Opens a connection: ``(url, verify_ssl, call_timeout)``.
ConnectionFactory = Callable[[str, bool, float], _Connection]


def _open_connection(
    url: str, verify_ssl: bool, call_timeout: float
) -> _Connection:
    return jsonrpc.Connection.open(
        url, verify_ssl=verify_ssl, call_timeout=call_timeout
    )


class _Settings(NamedTuple):
    """What ``connect`` was given, kept for reconnecting."""

    url: str
    username: str
    api_key: str
    verify_ssl: bool
    timeout: float
    mechanism: str
    channel_binding: bool | None


class TrueNASClient:
    """A logged-in session with one TrueNAS system."""

    def __init__(
        self, connection_factory: ConnectionFactory = _open_connection
    ) -> None:
        self._open_connection = connection_factory
        self._conn: _Connection | None = None
        self._settings: _Settings | None = None
        # Serializes reconnecting, so concurrent calls open one connection.
        self._lock = threading.Lock()

    # -- connection --------------------------------------------------------

    @staticmethod
    def dependency_available() -> bool:
        """Whether ``websocket-client`` is installed."""
        return DEPENDENCY_AVAILABLE

    @staticmethod
    def dependency_error() -> str | None:
        """Why ``websocket-client`` could not be imported, if it could not.

        Distinguishes a missing package from a present but broken one.
        """
        return _import_error

    def connect(
        self,
        url: str,
        username: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        timeout: float = jsonrpc.CALL_TIMEOUT,
        auth_mechanism: str = AUTH_PLAIN,
        channel_binding: bool | None = None,
    ) -> None:
        """Open the connection and log in.

        ``url`` is the full endpoint, e.g. ``wss://host/api/current``.
        ``auth_mechanism`` is ``PLAIN`` or ``SCRAM`` (see ``auth``).
        ``channel_binding`` applies to SCRAM: None binds the login to the
        TLS certificate whenever the connection uses TLS, True requires
        that, and False turns it off.
        """
        mechanism = auth_mechanism.upper()
        if mechanism not in AUTH_MECHANISMS:
            raise TrueNASConnectionError(
                f'Invalid authentication mechanism {auth_mechanism!r}; '
                f'expected one of {", ".join(AUTH_MECHANISMS)}.'
            )
        self.close()
        settings = _Settings(
            url,
            username,
            api_key,
            verify_ssl,
            timeout,
            mechanism,
            channel_binding,
        )
        with self._lock:
            self._conn = self._log_in(settings)
            self._settings = settings

    def _log_in(self, settings: _Settings) -> _Connection:
        """Open a connection and log it in."""
        conn = self._open_connection(
            settings.url, settings.verify_ssl, settings.timeout
        )
        try:
            auth.login(
                conn,
                settings.username,
                settings.api_key,
                settings.mechanism,
                channel_binding=settings.channel_binding,
            )
        except Exception as exc:
            # Don't leave an unauthenticated connection open.
            conn.close()
            raise TrueNASConnectionError(
                f'Failed to authenticate to TrueNAS at {settings.url} using '
                f'{settings.mechanism}: {exc}'
            ) from exc
        return conn

    @staticmethod
    def api_versions(
        url: str, *, verify_ssl: bool = True, timeout: float = 10.0
    ) -> list[str]:
        """API versions the server advertises, via ``GET /api/versions``.

        Returns an empty list if the endpoint cannot be reached or parsed --
        the check is advisory, so an unreachable preflight must not stop a
        backend from starting.
        """
        scheme = 'https' if url.startswith('wss') else 'http'
        host = url.split('://', 1)[-1].split('/', 1)[0]
        context: ssl.SSLContext | None = None
        if scheme == 'https' and not verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(  # noqa: S310 - operator-configured
                f'{scheme}://{host}/api/versions',
                timeout=timeout,
                context=context,
            ) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, ValueError, OSError):
            return []
        if not isinstance(payload, list):
            return []
        return [v for v in cast('list[Any]', payload) if isinstance(v, str)]

    def close(self) -> None:
        """Close the connection if open (idempotent)."""
        with self._lock:
            conn = self._conn
            self._conn = None
            self._settings = None
        if conn is not None:
            conn.close()

    def ping(self) -> None:
        """Liveness check (``core.ping``)."""
        self._call('core.ping')

    # -- low-level call plumbing ------------------------------------------

    def _connection(self) -> _Connection:
        """The open connection, reconnecting and logging in if it dropped."""
        conn = self._conn
        if conn is not None and not conn.closed:
            return conn
        with self._lock:
            conn = self._conn
            if conn is not None and not conn.closed:
                return conn
            settings = self._settings
            if settings is None:
                raise TrueNASConnectionError(
                    'TrueNAS client is not connected.'
                )
            LOG.warning(
                'The connection to TrueNAS at %s was lost; reconnecting.',
                settings.url,
            )
            conn = self._log_in(settings)
            self._conn = conn
            return conn

    def _discard(self, conn: _Connection) -> None:
        """Forget ``conn`` so the next call opens a new connection."""
        with self._lock:
            if self._conn is conn:
                self._conn = None
        conn.close()

    def _call(self, method: str, *params: object) -> object:
        """Call ``method``, retrying once where that is safe.

        A call is sent again on a new connection only when TrueNAS cannot
        have run it -- the request never left, or TrueNAS refused it because
        the session had ended -- or when it only reads. A call that may have
        run is never repeated: creating a dataset twice is not harmless.
        """
        for attempt in (1, 2):
            conn = self._connection()
            try:
                return conn.call(method, *params)
            except TrueNASConnectionError as exc:
                self._discard(conn)
                retry_safe = not exc.request_sent or _is_read_only(method)
                if attempt == 1 and retry_safe:
                    continue
                raise
            except TrueNASApiError as exc:
                if attempt == 1 and exc.errno == jsonrpc.ENOTAUTHENTICATED:
                    LOG.warning('TrueNAS ended the session; logging in again.')
                    self._discard(conn)
                    continue
                raise
        raise AssertionError('unreachable')  # pragma: no cover

    def _query(
        self,
        method: str,
        filters: list[list[object]] | None = None,
        options: JsonDict | None = None,
    ) -> list[JsonDict]:
        result = self._call(method, filters or [], options or {})
        return cast('list[JsonDict]', result)

    def _query_one(
        self,
        method: str,
        filters: list[list[object]],
    ) -> JsonDict | None:
        rows = self._query(method, filters)
        return rows[0] if rows else None

    # -- datasets / zvols --------------------------------------------------

    def create_zvol(
        self,
        name: str,
        size_bytes: int,
        *,
        volblocksize: str,
        compression: str,
        sparse: bool,
        create_ancestors: bool = True,
    ) -> JsonDict:
        """``pool.dataset.create`` for a zvol."""
        options: JsonDict = {
            'name': name,
            'type': 'VOLUME',
            'volsize': size_bytes,
            'volblocksize': volblocksize,
            'compression': compression.upper(),
            'create_ancestors': create_ancestors,
        }
        if sparse:
            options['sparse'] = True
        return cast('JsonDict', self._call('pool.dataset.create', options))

    def get_dataset(self, path: str) -> JsonDict | None:
        """``pool.dataset.get_instance``; ``None`` if it does not exist."""
        try:
            return cast(
                'JsonDict',
                self._call(
                    'pool.dataset.get_instance',
                    path,
                    {
                        'extra': {
                            'properties': [
                                'volsize',
                                'refreservation',
                                'origin',
                            ]
                        }
                    },
                ),
            )
        except TrueNASNotFound:
            return None

    def update_dataset(self, path: str, updates: JsonDict) -> JsonDict:
        """``pool.dataset.update`` (e.g. ``volsize``, ``refreservation``)."""
        return cast(
            'JsonDict', self._call('pool.dataset.update', path, updates)
        )

    def delete_dataset(
        self,
        path: str,
        *,
        recursive: bool = False,
        force: bool = True,
    ) -> None:
        """``pool.dataset.delete``. ENOENT is treated as success.

        Non-recursive by default, and the driver never asks for recursive:
        on TrueNAS 27 a recursive delete is ``zfs destroy -R``, which also
        destroys every clone of the dataset -- other Cinder volumes. A
        non-recursive delete is refused while the dataset has snapshots, so
        it can never take a clone with it.
        """
        try:
            self._call(
                'pool.dataset.delete',
                path,
                {'recursive': recursive, 'force': force},
            )
        except TrueNASNotFound:
            return

    def promote_dataset(self, clone_path: str) -> None:
        """``pool.dataset.promote`` -- re-parent a clone onto itself.

        Net-new relative to the CSI driver (which never promotes). Moves the
        origin snapshots onto the clone so the former source dataset is no
        longer an origin and can be destroyed. Synchronous, returns ``null``.
        """
        self._call('pool.dataset.promote', clone_path)

    def _origin_of(self, row: JsonDict) -> str | None:
        origin = row.get('origin')
        if isinstance(origin, dict):
            origin_dict = cast('JsonDict', origin)
            for key in ('parsed', 'value', 'rawvalue'):
                val = origin_dict.get(key)
                if isinstance(val, str) and val:
                    return val
            return None
        if isinstance(origin, str) and origin:
            return origin
        return None

    def _clones_matching(
        self,
        pool: str,
        predicate: Callable[[str], bool],
    ) -> list[str]:
        rows = self._query(
            'pool.dataset.query',
            [['pool', '=', pool]],
            {'extra': {'flat': True, 'properties': ['origin']}},
        )
        clones: list[str] = []
        for row in rows:
            origin = self._origin_of(row)
            if origin is not None and predicate(origin):
                name = row.get('name') or row.get('id')
                if isinstance(name, str):
                    clones.append(name)
        return clones

    def clones_of_snapshot(self, snapshot_id: str) -> list[str]:
        """Dataset paths whose ZFS origin is exactly ``snapshot_id``."""
        pool = snapshot_id.split('/', 1)[0]
        return self._clones_matching(pool, lambda o: o == snapshot_id)

    def dependent_clones(self, dataset: str) -> list[str]:
        """Dataset paths cloned from any snapshot of ``dataset``."""
        pool = dataset.split('/', 1)[0]
        prefix = f'{dataset}@'
        return self._clones_matching(pool, lambda o: o.startswith(prefix))

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(
        self,
        dataset: str,
        name: str,
        *,
        recursive: bool = False,
    ) -> JsonDict:
        """``pool.snapshot.create`` -> id ``<dataset>@<name>``."""
        return cast(
            'JsonDict',
            self._call(
                'pool.snapshot.create',
                {'dataset': dataset, 'name': name, 'recursive': recursive},
            ),
        )

    def delete_snapshot(
        self,
        snapshot_id: str,
        *,
        defer: bool = False,
        recursive: bool = False,
    ) -> None:
        """``pool.snapshot.delete``.

        ``defer=True`` marks a snapshot with dependent clones for removal once
        its last clone is gone (it disappears from listings immediately).
        ENOENT is treated as success.
        """
        try:
            self._call(
                'pool.snapshot.delete',
                snapshot_id,
                {'defer': defer, 'recursive': recursive},
            )
        except TrueNASNotFound:
            return

    def clone_snapshot(self, snapshot_id: str, dataset_dst: str) -> None:
        """``pool.snapshot.clone`` -- create ``dataset_dst`` from a snap."""
        self._call(
            'pool.snapshot.clone',
            {'snapshot': snapshot_id, 'dataset_dst': dataset_dst},
        )

    def list_snapshots(self, dataset: str) -> list[str]:
        """Ids of the snapshots taken of ``dataset`` itself."""
        rows = self._query('pool.snapshot.query', [['dataset', '=', dataset]])
        return [row['id'] for row in rows if isinstance(row.get('id'), str)]

    def get_snapshot(self, snapshot_id: str) -> JsonDict | None:
        """Look up a snapshot by full id; ``None`` if absent."""
        return self._query_one(
            'pool.snapshot.query', [['id', '=', snapshot_id]]
        )

    def rollback_snapshot(
        self, snapshot_id: str, *, force: bool = True
    ) -> None:
        """``pool.snapshot.rollback`` (ZFS revert; latest-snapshot only)."""
        self._call('pool.snapshot.rollback', snapshot_id, {'force': force})

    # -- iSCSI global configuration ---------------------------------------

    def iscsi_global_config(self) -> JsonDict:
        """``iscsi.global.config`` -- system-wide iSCSI settings.

        ``basename`` is the IQN prefix TrueNAS puts in front of every target
        name, and ``listen_port`` is the default iSCSI port.
        """
        return cast('JsonDict', self._call('iscsi.global.config'))

    # -- iSCSI portal ------------------------------------------------------

    def get_portal(self, portal_id: int) -> JsonDict | None:
        """``iscsi.portal.query`` by id."""
        return self._query_one('iscsi.portal.query', [['id', '=', portal_id]])

    def portal_listen_addresses(
        self, portal_id: int
    ) -> list[tuple[str, int | None]]:
        """The portal's listen addresses as ``(ip, port)`` pairs.

        A ``0.0.0.0`` (or ``::``) wildcard bind is returned as-is for the
        driver to resolve. ``port`` is ``None`` when an entry omits it.
        """
        portal = self.get_portal(portal_id)
        if portal is None:
            return []
        listen = portal.get('listen')
        addresses: list[tuple[str, int | None]] = []
        if isinstance(listen, list):
            for entry in cast('list[Any]', listen):
                if not isinstance(entry, dict):
                    continue
                fields = cast('JsonDict', entry)
                ip = fields.get('ip')
                if not isinstance(ip, str) or not ip:
                    continue
                port = fields.get('port')
                valid = isinstance(port, int) and not isinstance(port, bool)
                addresses.append((ip, port if valid else None))
        return addresses

    # -- iSCSI target ------------------------------------------------------

    def find_target_by_name(self, name: str) -> JsonDict | None:
        """``iscsi.target.query`` by name."""
        return self._query_one('iscsi.target.query', [['name', '=', name]])

    def get_target(self, target_id: int) -> JsonDict | None:
        """``iscsi.target.query`` by id."""
        return self._query_one('iscsi.target.query', [['id', '=', target_id]])

    def create_target(
        self,
        name: str,
        alias: str,
        groups: list[JsonDict],
    ) -> JsonDict:
        """``iscsi.target.create``.

        ``groups`` entries look like
        ``{'portal': <id>, 'authmethod': 'CHAP'|'NONE', 'auth': <tag|None>,
        'initiator': <initiator_id|None>}``.
        """
        return cast(
            'JsonDict',
            self._call(
                'iscsi.target.create',
                {
                    'name': name,
                    'alias': alias,
                    'mode': 'ISCSI',
                    'groups': groups,
                },
            ),
        )

    def update_target(self, target_id: int, updates: JsonDict) -> JsonDict:
        """``iscsi.target.update`` -- used to adjust access groups."""
        return cast(
            'JsonDict',
            self._call('iscsi.target.update', target_id, updates),
        )

    def delete_target(
        self,
        target_id: int,
        *,
        force: bool = True,
        delete_extents: bool = False,
    ) -> None:
        """``iscsi.target.delete [id, force, delete_extents]``."""
        try:
            self._call('iscsi.target.delete', target_id, force, delete_extents)
        except TrueNASNotFound:
            return

    # -- iSCSI extent ------------------------------------------------------

    def find_extent_by_disk(self, zvol_path: str) -> JsonDict | None:
        """``iscsi.extent.query`` by backing disk (``zvol/<dataset>``)."""
        return self._query_one(
            'iscsi.extent.query', [['disk', '=', zvol_path]]
        )

    def create_extent(
        self,
        name: str,
        zvol_path: str,
        *,
        blocksize: int = common.EXTENT_BLOCKSIZE,
    ) -> JsonDict:
        """``iscsi.extent.create`` for a zvol-backed DISK extent."""
        return cast(
            'JsonDict',
            self._call(
                'iscsi.extent.create',
                {
                    'name': name,
                    'type': 'DISK',
                    'disk': zvol_path,
                    'blocksize': blocksize,
                    'enabled': True,
                },
            ),
        )

    def delete_extent(
        self,
        extent_id: int,
        *,
        remove: bool = False,
        force: bool = True,
    ) -> None:
        """``iscsi.extent.delete [id, remove, force]``."""
        try:
            self._call('iscsi.extent.delete', extent_id, remove, force)
        except TrueNASNotFound:
            return

    # -- iSCSI target-extent association ----------------------------------

    def find_targetextent(
        self,
        *,
        target_id: int | None = None,
        extent_id: int | None = None,
    ) -> JsonDict | None:
        """``iscsi.targetextent.query`` by target and/or extent id."""
        filters: list[list[object]] = []
        if target_id is not None:
            filters.append(['target', '=', target_id])
        if extent_id is not None:
            filters.append(['extent', '=', extent_id])
        return self._query_one('iscsi.targetextent.query', filters)

    def list_targetextents_for_target(
        self,
        target_id: int,
    ) -> list[JsonDict]:
        """All target-extent rows for a target."""
        return self._query(
            'iscsi.targetextent.query', [['target', '=', target_id]]
        )

    def create_targetextent(
        self,
        target_id: int,
        extent_id: int,
        *,
        lunid: int = common.LUN_ID,
    ) -> JsonDict:
        """``iscsi.targetextent.create``."""
        return cast(
            'JsonDict',
            self._call(
                'iscsi.targetextent.create',
                {'target': target_id, 'extent': extent_id, 'lunid': lunid},
            ),
        )

    def delete_targetextent(
        self,
        te_id: int,
        *,
        force: bool = True,
    ) -> None:
        """``iscsi.targetextent.delete [id, force]``."""
        try:
            self._call('iscsi.targetextent.delete', te_id, force)
        except TrueNASNotFound:
            return

    # -- iSCSI CHAP auth ---------------------------------------------------

    def next_auth_tag(self) -> int:
        """Compute the next free CHAP auth tag (``max(tags) + 1``)."""
        rows = self._query('iscsi.auth.query')
        max_tag = 0
        for row in rows:
            tag = row.get('tag')
            if isinstance(tag, int) and tag > max_tag:
                max_tag = tag
        return max_tag + 1

    def find_auth_by_tag(self, tag: int) -> JsonDict | None:
        """``iscsi.auth.query`` by tag."""
        return self._query_one('iscsi.auth.query', [['tag', '=', tag]])

    def find_auth_by_user(self, user: str) -> JsonDict | None:
        """``iscsi.auth.query`` by CHAP user (idempotent re-create key)."""
        return self._query_one('iscsi.auth.query', [['user', '=', user]])

    def create_auth(
        self,
        tag: int,
        user: str,
        secret: str,
        *,
        peeruser: str | None = None,
        peersecret: str | None = None,
    ) -> JsonDict:
        """``iscsi.auth.create``. Mutual CHAP adds peer credentials."""
        options: JsonDict = {'tag': tag, 'user': user, 'secret': secret}
        if peeruser is not None and peersecret is not None:
            options['peeruser'] = peeruser
            options['peersecret'] = peersecret
        return cast('JsonDict', self._call('iscsi.auth.create', options))

    def delete_auth(self, auth_id: int) -> None:
        """``iscsi.auth.delete [id]``."""
        try:
            self._call('iscsi.auth.delete', auth_id)
        except TrueNASNotFound:
            return

    # -- iSCSI initiator group (multiattach access control) ---------------

    def find_initiator_by_comment(self, comment: str) -> JsonDict | None:
        """``iscsi.initiator.query`` by comment (one group per target)."""
        return self._query_one(
            'iscsi.initiator.query', [['comment', '=', comment]]
        )

    def get_initiator(self, initiator_id: int) -> JsonDict | None:
        """``iscsi.initiator.query`` by id."""
        return self._query_one(
            'iscsi.initiator.query', [['id', '=', initiator_id]]
        )

    def create_initiator(
        self,
        initiators: list[str],
        comment: str,
    ) -> JsonDict:
        """``iscsi.initiator.create`` -- allowed-initiator IQN group."""
        return cast(
            'JsonDict',
            self._call(
                'iscsi.initiator.create',
                {'initiators': initiators, 'comment': comment},
            ),
        )

    def update_initiator(
        self,
        initiator_id: int,
        initiators: list[str],
    ) -> JsonDict:
        """``iscsi.initiator.update`` -- replace the allowed IQN list."""
        return cast(
            'JsonDict',
            self._call(
                'iscsi.initiator.update',
                initiator_id,
                {'initiators': initiators},
            ),
        )

    def delete_initiator(self, initiator_id: int) -> None:
        """``iscsi.initiator.delete [id]``."""
        try:
            self._call('iscsi.initiator.delete', initiator_id)
        except TrueNASNotFound:
            return

    # -- capacity ----------------------------------------------------------

    def get_pool(self, pool: str) -> JsonDict | None:
        """``pool.query`` by name (total size, health)."""
        return self._query_one('pool.query', [['name', '=', pool]])

    def pool_space(self, pool: str) -> PoolSpace:
        """Used and available bytes of ``pool``'s root dataset.

        ``used + available`` is the usable capacity. ``pool.query``'s
        ``size`` is the raw vdev size instead, which counts RAIDZ parity and
        the space ZFS holds back. Uses ``zfs.resource.query``; note its
        single-options-object param shape (not ``[filters, opts]``).
        """
        result = self._call(
            'zfs.resource.query',
            {
                'paths': [pool],
                'properties': ['used', 'available'],
                'get_source': False,
            },
        )
        rows = cast('list[JsonDict]', result)
        props = rows[0].get('properties') if rows else None
        if not isinstance(props, dict):
            return PoolSpace(used=0, available=0)
        props_dict = cast('JsonDict', props)
        return PoolSpace(
            used=self._property_bytes(props_dict, 'used'),
            available=self._property_bytes(props_dict, 'available'),
        )

    @classmethod
    def _property_bytes(cls, props: JsonDict, name: str) -> int:
        prop = props.get(name)
        if isinstance(prop, dict):
            return cls._parse_size(cast('JsonDict', prop).get('value'))
        return 0

    def provisioned_bytes(self, pool: str) -> int:
        """Sum of ``volsize`` over every zvol in ``pool``.

        Includes zvols Cinder did not create: Cinder's provisioned capacity
        is everything provisioned on the backend. Filesystem datasets carry
        no ``volsize`` and are skipped.
        """
        rows = self._query(
            'pool.dataset.query',
            [['pool', '=', pool]],
            {'extra': {'flat': True, 'properties': ['volsize']}},
        )
        total = 0
        for row in rows:
            volsize = row.get('volsize')
            if isinstance(volsize, dict):
                total += self._parse_size(
                    cast('JsonDict', volsize).get('parsed')
                )
        return total

    @staticmethod
    def _parse_size(value: object) -> int:
        """Coerce a ZFS size property (int/float/str) to bytes."""
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0
