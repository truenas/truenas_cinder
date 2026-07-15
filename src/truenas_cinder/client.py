# Copyright (c) 2026 TrueNAS, Inc.
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
"""Typed adapter over ``truenas_api_client``.

This is the single place the untyped/LGPL ``truenas_api_client`` is imported.
The import is lazy (RBD model): if the package is missing the driver still
loads and reports the failure from ``check_for_setup_error``. Every TrueNAS
JSON-RPC call the driver makes goes through a typed method here so the rest of
the codebase never touches raw dicts of ``Any``.

All TrueNAS calls are synchronous JSON-RPC 2.0 requests -- matching the
production TrueNAS CSI driver, which uses no ``job=true`` polling for any of
the dataset/snapshot/iscsi methods used here.
"""

import errno as _errno
from collections.abc import Callable
from typing import Any, Protocol, cast

from truenas_cinder import common
from truenas_cinder.exception import (
    TrueNASApiError,
    TrueNASClientError,
    TrueNASConnectionError,
    TrueNASNotFound,
)

# --- Lazy import of the LGPL client (RBD pattern) -------------------------
try:
    from truenas_api_client import Client as _client_factory
    from truenas_api_client.exc import ClientException as _ClientException
except ImportError:  # pragma: no cover - exercised via check_for_setup_error
    _client_factory = None
    _ClientException = None

#: True when ``truenas_api_client`` is importable in this environment.
DEPENDENCY_AVAILABLE: bool = _client_factory is not None

#: Type alias for a decoded TrueNAS JSON object.
JsonDict = dict[str, Any]


class _RawClient(Protocol):
    """The subset of ``truenas_api_client.Client`` the adapter uses.

    The concrete client exposes these through ``__getattr__`` delegation, so
    we describe the contract explicitly and ``cast`` to it -- that keeps every
    call site precisely typed instead of ``Any``.
    """

    def call(self, method: str, *params: object) -> object: ...

    def login_with_api_key(
        self,
        username: str,
        api_key: str,
        *,
        channel_binding: bool = ...,
    ) -> None: ...

    def close(self) -> None: ...


class TrueNASClient:
    """Stateful wrapper around one long-lived TrueNAS WebSocket connection."""

    def __init__(self) -> None:
        self._client: _RawClient | None = None

    # -- connection --------------------------------------------------------

    @staticmethod
    def dependency_available() -> bool:
        """Whether the ``truenas_api_client`` dependency is installed."""
        return DEPENDENCY_AVAILABLE

    def connect(
        self,
        url: str,
        username: str,
        api_key: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 60.0,
        channel_binding: bool | None = None,
    ) -> None:
        """Open the WebSocket connection and authenticate.

        ``url`` is the full endpoint, e.g. ``wss://host/api/current``.
        ``channel_binding`` defaults to ``verify_ssl`` (binding requires a
        verified TLS chain); it can be disabled for self-signed setups.
        """
        if _client_factory is None:
            raise TrueNASConnectionError(
                'truenas_api_client is not installed; install it to use the '
                'TrueNAS Cinder driver.'
            )
        if channel_binding is None:
            channel_binding = verify_ssl
        try:
            raw = cast(
                '_RawClient',
                _client_factory(
                    uri=url, verify_ssl=verify_ssl, call_timeout=timeout
                ),
            )
            raw.login_with_api_key(
                username, api_key, channel_binding=channel_binding
            )
        except Exception as exc:
            raise TrueNASConnectionError(
                f'Failed to connect/authenticate to TrueNAS at {url}: {exc}'
            ) from exc
        self._client = raw

    def close(self) -> None:
        """Close the connection if open (idempotent)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._client = None

    def ping(self) -> None:
        """Liveness check (``core.ping``)."""
        self._call('core.ping')

    # -- low-level call plumbing ------------------------------------------

    def _call(self, method: str, *params: object) -> object:
        if self._client is None:
            raise TrueNASConnectionError('TrueNAS client is not connected.')
        try:
            return self._client.call(method, *params)
        except Exception as exc:
            raise self._translate(method, exc) from exc

    @staticmethod
    def _translate(method: str, exc: Exception) -> TrueNASClientError:
        """Map a raw client exception to the driver's typed hierarchy."""
        if _ClientException is not None and isinstance(exc, _ClientException):
            errno_val: int | None = exc.errno
            message = f'{method} failed: {exc.error}'
            if errno_val == _errno.ENOENT:
                return TrueNASNotFound(message, errno=errno_val)
            return TrueNASApiError(message, errno=errno_val)
        return TrueNASClientError(f'{method} failed: {exc}')

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
        recursive: bool = True,
        force: bool = True,
    ) -> None:
        """``pool.dataset.delete``. ENOENT is treated as success."""
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

    # -- iSCSI portal ------------------------------------------------------

    def get_portal(self, portal_id: int) -> JsonDict | None:
        """``iscsi.portal.query`` by id."""
        return self._query_one('iscsi.portal.query', [['id', '=', portal_id]])

    def portal_listen_ips(self, portal_id: int) -> list[str]:
        """The portal's configured listen IP addresses.

        A ``0.0.0.0`` (or ``::``) wildcard bind is returned as-is; the driver
        resolves it to a routable address.
        """
        portal = self.get_portal(portal_id)
        if portal is None:
            return []
        listen = portal.get('listen')
        ips: list[str] = []
        if isinstance(listen, list):
            for entry in cast('list[Any]', listen):
                if isinstance(entry, dict):
                    ip = cast('JsonDict', entry).get('ip')
                    if isinstance(ip, str) and ip:
                        ips.append(ip)
        return ips

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

    def available_bytes(self, pool: str) -> int:
        """Free bytes on ``pool`` via ``zfs.resource.query``.

        Note the single-options-object param shape (not ``[filters, opts]``).
        """
        result = self._call(
            'zfs.resource.query',
            {
                'paths': [pool],
                'properties': ['available'],
                'get_source': False,
            },
        )
        rows = cast('list[JsonDict]', result)
        if not rows:
            return 0
        props = rows[0].get('properties')
        if isinstance(props, dict):
            available = cast('JsonDict', props).get('available')
            if isinstance(available, dict):
                return self._parse_size(
                    cast('JsonDict', available).get('value')
                )
        return 0

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
