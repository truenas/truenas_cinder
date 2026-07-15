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
"""TrueNAS SCALE iSCSI volume driver for OpenStack Cinder.

Control-plane only: provisions zvols/extents/targets on TrueNAS over the
WebSocket JSON-RPC API and returns iSCSI connection info. All host-side
attach/detach/multipath is handled by os-brick.
"""

from typing import Any, Protocol, cast

from oslo_log import log as logging

from cinder import coordination, exception, interface
from cinder.volume import driver, volume_utils
from truenas_cinder import client as tn_client
from truenas_cinder import common, opts

LOG = logging.getLogger(__name__)

ISCSI_PORT = 3260
_WILDCARD_IPS = frozenset({'0.0.0.0', '::'})
# Safety bound on the promote loop when detaching dependent clones.
_MAX_PROMOTE_ROUNDS = 64


class _VolumeObj(Protocol):
    """The Cinder Volume attributes the driver reads.

    Cinder ships no type stubs; this Protocol documents exactly which fields
    the driver depends on (and lets basedpyright check every access). Members
    are read-only (the driver never mutates the object) so the Protocol is
    covariant and plain-attribute objects satisfy it.
    """

    @property
    def id(self) -> str: ...
    @property
    def name_id(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def provider_location(self) -> str | None: ...
    @property
    def provider_auth(self) -> str | None: ...


class _SnapshotObj(Protocol):
    """The Cinder Snapshot attributes the driver reads."""

    @property
    def id(self) -> str: ...
    @property
    def volume_id(self) -> str: ...
    @property
    def volume_size(self) -> int: ...
    @property
    def volume(self) -> _VolumeObj: ...


@interface.volumedriver
class TrueNASISCSIDriver(driver.ISCSIDriver):
    """iSCSI driver for TrueNAS SCALE.

    Version history:
        1.0.0 - Initial iSCSI driver (create/delete/attach/extend/snapshot/
                clone/from-snapshot/from-volume, multipath, multiattach).
    """

    VERSION = '1.0.0'
    # Must match the third-party CI wiki page name exactly.
    CI_WIKI_NAME = 'TrueNAS_CI'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.configuration.append_config_values(opts.truenas_opts)
        self._client: tn_client.TrueNASClient = tn_client.TrueNASClient()
        self._stats: dict[str, Any] = {}
        # Populated from config in do_setup().
        self._pool: str = ''
        self._dataset_root: str = ''
        self._portal_id: int = 1
        self._sparse: bool = True
        self._volblocksize: str = '16K'
        self._compression: str = 'LZ4'
        self._verify_ssl: bool = True
        self._iqn_base: str = ''
        self._use_chap: bool = False
        self._backend_name: str = 'TrueNAS_iSCSI'
        self._host_address: str = ''
        self._api_url: str = ''
        self._portal_ips: list[str] = []

    @classmethod
    def get_driver_options(cls) -> list[Any]:
        """Options this driver consumes (its own plus inherited base opts)."""
        additional = cls._get_oslo_driver_opts(
            'target_prefix',
            'use_chap_auth',
            'volume_backend_name',
            'reserved_percentage',
            'max_over_subscription_ratio',
        )
        return opts.truenas_opts + additional

    # -- lifecycle ---------------------------------------------------------

    def do_setup(self, context: Any) -> None:
        conf = self.configuration
        self._pool = str(conf.safe_get('truenas_pool') or '')
        self._dataset_root = str(conf.safe_get('truenas_dataset_root') or '')
        self._portal_id = int(conf.safe_get('truenas_iscsi_portal_id') or 1)
        self._sparse = bool(conf.safe_get('truenas_sparse'))
        self._volblocksize = str(conf.safe_get('truenas_volblocksize'))
        self._compression = str(conf.safe_get('truenas_compression'))
        self._verify_ssl = bool(conf.safe_get('truenas_verify_ssl'))
        self._iqn_base = str(conf.safe_get('target_prefix') or '')
        self._use_chap = bool(conf.safe_get('use_chap_auth'))
        self._backend_name = str(
            conf.safe_get('volume_backend_name') or 'TrueNAS_iSCSI'
        )
        self._host_address = str(conf.safe_get('san_ip') or '')
        self._api_url = str(
            conf.safe_get('truenas_api_url') or self._default_url()
        )

        if not tn_client.TrueNASClient.dependency_available():
            # check_for_setup_error() raises with an operator-friendly message.
            return

        username = str(conf.safe_get('san_login') or 'root')
        api_key = str(conf.safe_get('truenas_api_key') or '')
        self._client.connect(
            self._api_url, username, api_key, verify_ssl=self._verify_ssl
        )
        self._portal_ips = self._resolve_portal_ips()

    def _default_url(self) -> str:
        return f'wss://{self._host_address}/api/current'

    def check_for_setup_error(self) -> None:
        if not tn_client.TrueNASClient.dependency_available():
            raise exception.VolumeBackendAPIException(
                data='truenas_api_client is not installed. Install it on the '
                'cinder-volume host to use the TrueNAS driver.'
            )
        if not self._pool:
            raise exception.VolumeBackendAPIException(
                data='truenas_pool must be configured.'
            )
        if not self._iqn_base:
            raise exception.VolumeBackendAPIException(
                data='target_prefix (the iSCSI IQN base) must be configured.'
            )
        if self._client.get_pool(self._pool) is None:
            raise exception.VolumeBackendAPIException(
                data=f'TrueNAS pool "{self._pool}" was not found.'
            )
        if self._client.get_portal(self._portal_id) is None:
            raise exception.VolumeBackendAPIException(
                data=f'TrueNAS iSCSI portal {self._portal_id} was not found.'
            )

    # -- helpers -----------------------------------------------------------

    def _dataset_for(self, volume: _VolumeObj) -> str:
        return common.dataset_name(
            self._pool, self._dataset_root, volume.name_id
        )

    def _alias(self, volume: _VolumeObj) -> str:
        return f'Cinder volume {volume.name_id}'

    def _resolve_portal_ips(self) -> list[str]:
        ips = self._client.portal_listen_ips(self._portal_id)
        resolved: list[str] = []
        for ip in ips:
            if ip in _WILDCARD_IPS and self._host_address:
                resolved.append(self._host_address)
            else:
                resolved.append(ip)
        if not resolved and self._host_address:
            resolved = [self._host_address]
        return resolved

    @staticmethod
    def _parsed_int(dataset: dict[str, Any], key: str) -> int:
        prop = dataset.get(key)
        if isinstance(prop, dict):
            value = cast('dict[str, Any]', prop).get('parsed')
            if isinstance(value, bool):
                return 0
            if isinstance(value, (int, float)):
                return int(value)
        if isinstance(prop, bool):
            return 0
        if isinstance(prop, (int, float)):
            return int(prop)
        return 0

    def _is_thick(self, dataset: dict[str, Any]) -> bool:
        return self._parsed_int(dataset, 'refreservation') > 0

    # -- volume CRUD -------------------------------------------------------

    def create_volume(self, volume: _VolumeObj) -> dict[str, Any]:
        dataset = self._dataset_for(volume)
        size_bytes = common.gib_to_bytes(volume.size)
        self._client.create_zvol(
            dataset,
            size_bytes,
            volblocksize=self._volblocksize,
            compression=self._compression,
            sparse=self._sparse,
        )
        return {'provider_location': dataset}

    def delete_volume(self, volume: _VolumeObj) -> None:
        dataset = self._dataset_for(volume)
        if self._client.get_dataset(dataset) is None:
            return
        # Tear down any lingering iSCSI export first (best effort).
        self._teardown_iscsi_chain(volume)
        # Detach dependent clones so ZFS will let the source be destroyed.
        self._promote_dependent_clones(dataset)
        self._client.delete_dataset(dataset, recursive=True, force=True)

    def _promote_dependent_clones(self, dataset: str) -> None:
        """Re-parent every clone rooted on this dataset's snapshots.

        Iterates because promoting one clone can move snapshots between
        datasets; each round frees at least the promoted clone's origin, so
        the set converges (bounded by ``_MAX_PROMOTE_ROUNDS``).
        """
        for _ in range(_MAX_PROMOTE_ROUNDS):
            clones = self._client.dependent_clones(dataset)
            if not clones:
                return
            self._client.promote_dataset(clones[0])
        LOG.warning(
            'Dependent clones of %s did not fully detach after %d promote '
            'rounds.',
            dataset,
            _MAX_PROMOTE_ROUNDS,
        )

    def extend_volume(self, volume: _VolumeObj, new_size: int) -> None:
        dataset = self._dataset_for(volume)
        size_bytes = common.gib_to_bytes(new_size)
        updates: dict[str, Any] = {'volsize': size_bytes}
        existing = self._client.get_dataset(dataset)
        if existing is not None and self._is_thick(existing):
            updates['refreservation'] = size_bytes
        self._client.update_dataset(dataset, updates)

    def _resize_if_needed(
        self,
        volume: _VolumeObj,
        dataset: str,
        source_size_gib: int,
    ) -> None:
        if volume.size <= source_size_gib:
            return
        self.extend_volume(volume, volume.size)

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(self, snapshot: _SnapshotObj) -> None:
        dataset = self._dataset_for(snapshot.volume)
        name = common.snapshot_name(snapshot.id)
        snap_id = common.snapshot_id(dataset, name)
        if self._client.get_snapshot(snap_id) is not None:
            return
        self._client.create_snapshot(dataset, name)

    def delete_snapshot(self, snapshot: _SnapshotObj) -> None:
        dataset = self._dataset_for(snapshot.volume)
        snap_id = common.snapshot_id(
            dataset, common.snapshot_name(snapshot.id)
        )
        has_clones = bool(self._client.clones_of_snapshot(snap_id))
        # defer=True hides a snapshot that still has clones; it is removed
        # automatically once its last clone is destroyed.
        self._client.delete_snapshot(snap_id, defer=has_clones)

    def revert_to_snapshot(
        self,
        context: Any,
        volume: _VolumeObj,
        snapshot: _SnapshotObj,
    ) -> None:
        dataset = self._dataset_for(volume)
        snap_id = common.snapshot_id(
            dataset, common.snapshot_name(snapshot.id)
        )
        self._client.rollback_snapshot(snap_id, force=True)

    def create_volume_from_snapshot(
        self,
        volume: _VolumeObj,
        snapshot: _SnapshotObj,
    ) -> dict[str, Any]:
        src_dataset = self._dataset_for(snapshot.volume)
        snap_id = common.snapshot_id(
            src_dataset, common.snapshot_name(snapshot.id)
        )
        dst_dataset = self._dataset_for(volume)
        # Idempotent self-heal: a prior interrupted call may have created the
        # clone already; do not clone twice.
        if self._client.get_dataset(dst_dataset) is not None:
            self._resize_if_needed(volume, dst_dataset, snapshot.volume_size)
            return {'provider_location': dst_dataset}
        self._client.clone_snapshot(snap_id, dst_dataset)
        try:
            self._resize_if_needed(volume, dst_dataset, snapshot.volume_size)
        except Exception:
            # Reverse-compensate: drop the clone we just created. The source
            # snapshot is a first-class Cinder object -- never delete it.
            self._client.delete_dataset(dst_dataset)
            raise
        return {'provider_location': dst_dataset}

    def create_cloned_volume(
        self,
        volume: _VolumeObj,
        src_vref: _VolumeObj,
    ) -> dict[str, Any]:
        src_dataset = self._dataset_for(src_vref)
        dst_dataset = self._dataset_for(volume)
        snap_name = f'clone-{volume.name_id}'
        snap_id = common.snapshot_id(src_dataset, snap_name)

        # Idempotent self-heal: if the clone already exists (interrupted
        # prior call), don't re-snapshot or re-clone.
        if self._client.get_dataset(dst_dataset) is not None:
            self._resize_if_needed(volume, dst_dataset, src_vref.size)
            return {'provider_location': dst_dataset}

        if self._client.get_snapshot(snap_id) is None:
            self._client.create_snapshot(src_dataset, snap_name)
        try:
            self._client.clone_snapshot(snap_id, dst_dataset)
        except Exception:
            self._client.delete_snapshot(snap_id, defer=False)
            raise
        try:
            # Hide the intermediate snapshot; it survives as the clone's
            # (hidden) ZFS origin until the clone is deleted.
            self._client.delete_snapshot(snap_id, defer=True)
            self._resize_if_needed(volume, dst_dataset, src_vref.size)
        except Exception:
            self._client.delete_dataset(dst_dataset)
            self._client.delete_snapshot(snap_id, defer=False)
            raise
        return {'provider_location': dst_dataset}

    def update_migrated_volume(
        self,
        ctxt: Any,
        volume: _VolumeObj,
        new_volume: _VolumeObj,
        original_volume_status: str,
    ) -> dict[str, Any]:
        # Backend naming keys on name_id; rather than rename the dataset,
        # point the original volume record at the migrated backend volume.
        return {
            '_name_id': new_volume.name_id,
            'provider_location': new_volume.provider_location,
        }

    # -- export / connection ----------------------------------------------

    def ensure_export(
        self,
        context: Any,
        volume: _VolumeObj,
    ) -> dict[str, Any] | None:
        return self._create_export(volume)

    def create_export(
        self,
        context: Any,
        volume: _VolumeObj,
        connector: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._create_export(volume)

    def _create_export(self, volume: _VolumeObj) -> dict[str, Any] | None:
        chap_tag: int | None = None
        model: dict[str, Any] = {
            'provider_location': self._dataset_for(volume),
        }
        if self._use_chap:
            user, secret = self._chap_credentials(volume)
            chap_tag = self._ensure_chap_auth(user, secret)
            model['provider_auth'] = f'CHAP {user} {secret}'
        self._ensure_iscsi_chain(volume, chap_tag)
        return model

    def remove_export(self, context: Any, volume: _VolumeObj) -> None:
        self._teardown_iscsi_chain(volume)

    @coordination.synchronized('truenas-{volume.id}')
    def initialize_connection(
        self,
        volume: _VolumeObj,
        connector: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        chap_tag = self._existing_chap_tag(volume)
        target, _extent, _te, initiator = self._ensure_iscsi_chain(
            volume, chap_tag
        )
        host_iqn = connector.get('initiator')
        if isinstance(host_iqn, str) and host_iqn:
            self._add_host_initiator(initiator, host_iqn)
        return self._connection_info(volume, target, connector)

    @coordination.synchronized('truenas-{volume.id}')
    def terminate_connection(
        self,
        volume: _VolumeObj,
        connector: dict[str, Any] | None,
        **kwargs: Any,
    ) -> None:
        target_name = common.target_name(volume.name_id)
        group = self._client.find_initiator_by_comment(target_name)
        if group is None:
            return
        group_id = int(group['id'])
        if connector is None:
            # Detaching everywhere: clear the allowed-initiator list.
            self._client.update_initiator(group_id, [])
            return
        host_iqn = connector.get('initiator')
        if not isinstance(host_iqn, str) or not host_iqn:
            return
        current = self._initiator_iqns(group)
        if host_iqn in current:
            current.remove(host_iqn)
            self._client.update_initiator(group_id, current)

    # -- iSCSI chain management -------------------------------------------

    def _ensure_iscsi_chain(
        self,
        volume: _VolumeObj,
        chap_tag: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Idempotently assert target/extent/targetextent (+initiator group).

        Query-before-create at every step so an interrupted call self-heals
        (the CSI ``ensureISCSIChain`` pattern).
        """
        dataset = self._dataset_for(volume)
        zvol = common.zvol_device_path(dataset)
        tname = common.target_name(volume.name_id)
        ename = common.extent_name(volume.name_id)

        extent = self._client.find_extent_by_disk(zvol)
        if extent is None:
            extent = self._client.create_extent(ename, zvol)

        initiator = self._client.find_initiator_by_comment(tname)
        if initiator is None:
            initiator = self._client.create_initiator([], tname)

        target = self._client.find_target_by_name(tname)
        if target is None:
            group = self._target_group(chap_tag, int(initiator['id']))
            target = self._client.create_target(
                tname, self._alias(volume), [group]
            )

        te = self._client.find_targetextent(
            target_id=int(target['id']), extent_id=int(extent['id'])
        )
        if te is None:
            te = self._client.create_targetextent(
                int(target['id']), int(extent['id'])
            )
        return target, extent, te, initiator

    def _target_group(
        self,
        chap_tag: int | None,
        initiator_id: int,
    ) -> dict[str, Any]:
        group: dict[str, Any] = {
            'portal': self._portal_id,
            'initiator': initiator_id,
        }
        if chap_tag is not None:
            group['authmethod'] = 'CHAP'
            group['auth'] = chap_tag
        else:
            group['authmethod'] = 'NONE'
        return group

    def _teardown_iscsi_chain(self, volume: _VolumeObj) -> None:
        """Remove target/extent/targetextent/auth/initiator (leave zvol)."""
        dataset = self._dataset_for(volume)
        zvol = common.zvol_device_path(dataset)
        tname = common.target_name(volume.name_id)

        target = self._client.find_target_by_name(tname)
        extent = self._client.find_extent_by_disk(zvol)

        if target is not None:
            target_id = int(target['id'])
            for te in self._client.list_targetextents_for_target(target_id):
                self._client.delete_targetextent(int(te['id']))
            self._delete_target_auth(target)
            self._client.delete_target(target_id)
        if extent is not None:
            self._client.delete_extent(int(extent['id']))
        initiator = self._client.find_initiator_by_comment(tname)
        if initiator is not None:
            self._client.delete_initiator(int(initiator['id']))

    def _delete_target_auth(self, target: dict[str, Any]) -> None:
        """Delete the CHAP auth record referenced by a target's groups."""
        groups = target.get('groups')
        if not isinstance(groups, list):
            return
        for group in cast('list[Any]', groups):
            if not isinstance(group, dict):
                continue
            tag = cast('dict[str, Any]', group).get('auth')
            if isinstance(tag, int) and tag > 0:
                auth = self._client.find_auth_by_tag(tag)
                if auth is not None:
                    self._client.delete_auth(int(auth['id']))

    # -- CHAP --------------------------------------------------------------

    def _chap_credentials(self, volume: _VolumeObj) -> tuple[str, str]:
        if volume.provider_auth:
            parts = volume.provider_auth.split()
            if len(parts) == 3 and parts[0] == 'CHAP':
                return parts[1], parts[2]
        return common.target_name(volume.name_id), self._generate_chap_secret()

    @staticmethod
    def _generate_chap_secret() -> str:
        secret = volume_utils.generate_password(common.CHAP_SECRET_MAX_LEN)
        return secret[: common.CHAP_SECRET_MAX_LEN]

    def _ensure_chap_auth(self, user: str, secret: str) -> int:
        existing = self._client.find_auth_by_user(user)
        if existing is not None:
            return int(existing['tag'])
        tag = self._client.next_auth_tag()
        self._client.create_auth(tag, user, secret)
        return tag

    def _existing_chap_tag(self, volume: _VolumeObj) -> int | None:
        if not self._use_chap:
            return None
        user, secret = self._chap_credentials(volume)
        return self._ensure_chap_auth(user, secret)

    # -- connection dict ---------------------------------------------------

    def _connection_info(
        self,
        volume: _VolumeObj,
        target: dict[str, Any],
        connector: dict[str, Any],
    ) -> dict[str, Any]:
        iqn = common.target_iqn(
            self._iqn_base, common.target_name(volume.name_id)
        )
        portals = [f'{ip}:{ISCSI_PORT}' for ip in self._portal_ips]
        if not portals:
            raise exception.VolumeBackendAPIException(
                data=f'No listen IPs resolved for portal {self._portal_id}.'
            )

        data: dict[str, Any] = {
            'target_discovered': False,
            'target_iqn': iqn,
            'target_portal': portals[0],
            'target_lun': common.LUN_ID,
            'volume_id': volume.id,
            'discard': True,
        }
        if connector.get('multipath') or len(portals) > 1:
            data['target_iqns'] = [iqn] * len(portals)
            data['target_portals'] = portals
            data['target_luns'] = [common.LUN_ID] * len(portals)
        if self._use_chap and volume.provider_auth:
            parts = volume.provider_auth.split()
            if len(parts) == 3:
                data['auth_method'] = 'CHAP'
                data['auth_username'] = parts[1]
                data['auth_password'] = parts[2]
        return {'driver_volume_type': 'iscsi', 'data': data}

    # -- multiattach initiator management ---------------------------------

    @staticmethod
    def _initiator_iqns(initiator: dict[str, Any]) -> list[str]:
        raw = initiator.get('initiators')
        iqns: list[str] = []
        if isinstance(raw, list):
            for entry in cast('list[Any]', raw):
                if isinstance(entry, str):
                    iqns.append(entry)
        return iqns

    def _add_host_initiator(
        self,
        initiator: dict[str, Any],
        host_iqn: str,
    ) -> None:
        current = self._initiator_iqns(initiator)
        if host_iqn in current:
            return
        current.append(host_iqn)
        self._client.update_initiator(int(initiator['id']), current)

    # -- stats -------------------------------------------------------------

    def get_volume_stats(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or not self._stats:
            self._update_volume_stats()
        return self._stats

    def _update_volume_stats(self) -> None:
        free_bytes = self._client.available_bytes(self._pool)
        pool_info = self._client.get_pool(self._pool) or {}
        total_bytes = self._parsed_int(pool_info, 'size')
        allocated_bytes = self._parsed_int(pool_info, 'allocated')

        conf = self.configuration
        reserved = int(conf.safe_get('reserved_percentage') or 0)
        max_ratio = float(conf.safe_get('max_over_subscription_ratio') or 20.0)

        pool_stats: dict[str, Any] = {
            'pool_name': self._pool,
            'total_capacity_gb': common.bytes_to_gib(total_bytes),
            'free_capacity_gb': common.bytes_to_gib(free_bytes),
            'provisioned_capacity_gb': common.bytes_to_gib(allocated_bytes),
            'thin_provisioning_support': True,
            'thick_provisioning_support': True,
            'max_over_subscription_ratio': max_ratio,
            'reserved_percentage': reserved,
            'multiattach': True,
            'QoS_support': False,
        }
        self._stats = {
            'volume_backend_name': self._backend_name,
            'vendor_name': 'TrueNAS',
            'driver_version': self.VERSION,
            'storage_protocol': 'iSCSI',
            'pools': [pool_stats],
        }
