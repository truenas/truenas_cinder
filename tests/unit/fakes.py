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
"""Test doubles for the TrueNAS Cinder driver.

``FakeTrueNASClient`` is an in-memory stand-in for
``truenas_cinder.client.TrueNASClient`` that **models ZFS clone-dependency
semantics**. That modelling is deliberate: without it the clone/snapshot
lifecycle tests would pass vacuously while real hardware fails. Specifically
the fake:

* rejects ``delete_snapshot(defer=False)`` on a snapshot that has clones,
* rejects ``delete_dataset`` of a dataset that is still a clone origin,
* implements ``promote_dataset`` by re-parenting the origin snapshot.

Every call is recorded in ``calls`` so tests can assert exact sequences.
"""

from typing import Any

from truenas_cinder import common
from truenas_cinder.exception import TrueNASApiError

JsonDict = dict[str, Any]


class FakeConfig:
    """Minimal stand-in for cinder's Configuration object."""

    def __init__(self, **options: Any) -> None:
        self._options: dict[str, Any] = options

    def append_config_values(self, opts: list[Any]) -> None:
        return None

    def safe_get(self, key: str) -> Any:
        return self._options.get(key)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not set normally (e.g. option lookups).
        return self._options.get(name)


class FakeVolume:
    """Stand-in for a cinder Volume object."""

    def __init__(
        self,
        id: str,
        name_id: str | None = None,
        size: int = 1,
        provider_location: str | None = None,
        provider_auth: str | None = None,
    ) -> None:
        self.id: str = id
        self.name_id: str = name_id or id
        self.size: int = size
        self.provider_location: str | None = provider_location
        self.provider_auth: str | None = provider_auth


class FakeSnapshot:
    """Stand-in for a cinder Snapshot object."""

    def __init__(
        self,
        id: str,
        volume: FakeVolume,
        volume_size: int | None = None,
    ) -> None:
        self.id: str = id
        self.volume: FakeVolume = volume
        self.volume_id: str = volume.id
        self.volume_size: int = (
            volume_size if volume_size is not None else volume.size
        )


class FakeTrueNASClient:
    """In-memory TrueNAS client that honours ZFS clone-dependency rules."""

    def __init__(self) -> None:
        # dataset path -> {'volsize', 'refreservation', 'origin'}
        self.datasets: dict[str, JsonDict] = {}
        # snapshot id -> {'dataset', 'name', 'deferred'}
        self.snapshots: dict[str, JsonDict] = {}
        self.extents: dict[int, JsonDict] = {}
        self.targets: dict[int, JsonDict] = {}
        self.targetextents: dict[int, JsonDict] = {}
        self.auths: dict[int, JsonDict] = {}
        self.initiators: dict[int, JsonDict] = {}
        self._ids: dict[str, int] = {}
        self.calls: list[tuple[str, JsonDict]] = []
        self.pool: JsonDict = {
            'id': 1,
            'name': 'tank',
            'healthy': True,
            'size': 1000 * common.GIGABYTE,
            'allocated': 100 * common.GIGABYTE,
            'free': 900 * common.GIGABYTE,
        }
        self.portal: JsonDict = {
            'id': 1,
            'listen': [{'ip': '10.0.0.1', 'port': 3260}],
        }

    # -- test helpers ------------------------------------------------------

    def _record(self, method: str, /, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    def _next_id(self, kind: str) -> int:
        self._ids[kind] = self._ids.get(kind, 0) + 1
        return self._ids[kind]

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @staticmethod
    def dependency_available() -> bool:
        return True

    # -- datasets ----------------------------------------------------------

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
        self._record(
            'create_zvol',
            name=name,
            size_bytes=size_bytes,
            sparse=sparse,
            volblocksize=volblocksize,
            compression=compression,
        )
        if name in self.datasets:
            raise TrueNASApiError(f'dataset {name} exists')
        refreservation = 0 if sparse else size_bytes
        self.datasets[name] = {
            'volsize': size_bytes,
            'refreservation': refreservation,
            'origin': None,
        }
        return {'id': name, 'name': name}

    def get_dataset(self, path: str) -> JsonDict | None:
        self._record('get_dataset', path=path)
        ds = self.datasets.get(path)
        if ds is None:
            return None
        origin = ds['origin']
        return {
            'id': path,
            'name': path,
            'volsize': {'parsed': ds['volsize']},
            'refreservation': {'parsed': ds['refreservation']},
            'origin': {'parsed': origin or ''},
        }

    def update_dataset(self, path: str, updates: JsonDict) -> JsonDict:
        self._record('update_dataset', path=path, updates=dict(updates))
        ds = self.datasets[path]
        if 'volsize' in updates:
            ds['volsize'] = updates['volsize']
        if 'refreservation' in updates:
            ds['refreservation'] = updates['refreservation']
        return {'id': path, 'name': path}

    def delete_dataset(
        self,
        path: str,
        *,
        recursive: bool = True,
        force: bool = True,
    ) -> None:
        self._record(
            'delete_dataset', path=path, recursive=recursive, force=force
        )
        if path not in self.datasets:
            return
        # ZFS: a recursive (-r) destroy will not remove a dataset whose
        # snapshots are the origin of clones living elsewhere; only -R would
        # (which we never issue, since it would destroy other volumes).
        snaps_here = {
            sid for sid, s in self.snapshots.items() if s['dataset'] == path
        }
        for other, ds in self.datasets.items():
            if other != path and ds['origin'] in snaps_here:
                raise TrueNASApiError(
                    f'cannot destroy {path}: snapshot has dependent clones'
                )
        origin = self.datasets[path]['origin']
        del self.datasets[path]
        for sid in snaps_here:
            del self.snapshots[sid]
        # Deleting a clone may release a deferred origin snapshot that was
        # kept alive only by this clone.
        self._maybe_reap_deferred(origin)

    def _maybe_reap_deferred(self, snapshot_id: str | None) -> None:
        if snapshot_id is None:
            return
        snap = self.snapshots.get(snapshot_id)
        if snap is None or not snap['deferred']:
            return
        if not self._clones_of(snapshot_id):
            del self.snapshots[snapshot_id]

    def promote_dataset(self, clone_path: str) -> None:
        self._record('promote_dataset', clone_path=clone_path)
        clone = self.datasets[clone_path]
        origin = clone['origin']
        if origin is None:
            return
        snap = self.snapshots[origin]
        new_id = common.snapshot_id(clone_path, snap['name'])
        # Move the origin snapshot onto the promoted clone.
        self.snapshots[new_id] = {
            'dataset': clone_path,
            'name': snap['name'],
            'deferred': snap['deferred'],
        }
        del self.snapshots[origin]
        # Any dataset that referenced the old origin now references the moved
        # snapshot; the promoted clone becomes independent.
        for ds in self.datasets.values():
            if ds['origin'] == origin:
                ds['origin'] = new_id
        clone['origin'] = None
        # A deferred (destroy-pending) origin snapshot fires once it is no
        # longer holding any clone -- as ZFS does after promotion.
        self._maybe_reap_deferred(new_id)

    def _clones_of(self, snapshot_id: str) -> list[str]:
        return [
            path
            for path, ds in self.datasets.items()
            if ds['origin'] == snapshot_id
        ]

    def clones_of_snapshot(self, snapshot_id: str) -> list[str]:
        self._record('clones_of_snapshot', snapshot_id=snapshot_id)
        return self._clones_of(snapshot_id)

    def dependent_clones(self, dataset: str) -> list[str]:
        self._record('dependent_clones', dataset=dataset)
        result: list[str] = []
        for path, ds in self.datasets.items():
            origin = ds['origin']
            if origin is None:
                continue
            snap = self.snapshots.get(origin)
            if snap is not None and snap['dataset'] == dataset:
                result.append(path)
        return result

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(
        self,
        dataset: str,
        name: str,
        *,
        recursive: bool = False,
    ) -> JsonDict:
        self._record('create_snapshot', dataset=dataset, name=name)
        snap_id = common.snapshot_id(dataset, name)
        if snap_id in self.snapshots:
            raise TrueNASApiError(f'snapshot {snap_id} exists')
        self.snapshots[snap_id] = {
            'dataset': dataset,
            'name': name,
            'deferred': False,
        }
        return {'id': snap_id, 'dataset': dataset, 'name': name}

    def delete_snapshot(
        self,
        snapshot_id: str,
        *,
        defer: bool = False,
        recursive: bool = False,
    ) -> None:
        self._record('delete_snapshot', snapshot_id=snapshot_id, defer=defer)
        snap = self.snapshots.get(snapshot_id)
        if snap is None:
            return
        clones = self._clones_of(snapshot_id)
        if clones and not defer:
            raise TrueNASApiError(
                f'cannot destroy {snapshot_id}: snapshot has dependent clones'
            )
        if clones and defer:
            snap['deferred'] = True
            return
        del self.snapshots[snapshot_id]

    def clone_snapshot(self, snapshot_id: str, dataset_dst: str) -> None:
        self._record(
            'clone_snapshot', snapshot_id=snapshot_id, dataset_dst=dataset_dst
        )
        if snapshot_id not in self.snapshots:
            raise TrueNASApiError(f'no such snapshot {snapshot_id}')
        if dataset_dst in self.datasets:
            raise TrueNASApiError(f'dataset {dataset_dst} exists')
        src_dataset = self.snapshots[snapshot_id]['dataset']
        src = self.datasets[src_dataset]
        self.datasets[dataset_dst] = {
            'volsize': src['volsize'],
            'refreservation': 0,
            'origin': snapshot_id,
        }

    def get_snapshot(self, snapshot_id: str) -> JsonDict | None:
        self._record('get_snapshot', snapshot_id=snapshot_id)
        snap = self.snapshots.get(snapshot_id)
        # Deferred snapshots disappear from listings.
        if snap is None or snap['deferred']:
            return None
        return {'id': snapshot_id, 'dataset': snap['dataset']}

    def rollback_snapshot(
        self, snapshot_id: str, *, force: bool = True
    ) -> None:
        self._record('rollback_snapshot', snapshot_id=snapshot_id)
        if snapshot_id not in self.snapshots:
            raise TrueNASApiError(f'no such snapshot {snapshot_id}')

    # -- portal / pool -----------------------------------------------------

    def get_pool(self, pool: str) -> JsonDict | None:
        self._record('get_pool', pool=pool)
        return self.pool if pool == self.pool['name'] else None

    def get_portal(self, portal_id: int) -> JsonDict | None:
        self._record('get_portal', portal_id=portal_id)
        return self.portal if portal_id == self.portal['id'] else None

    def portal_listen_ips(self, portal_id: int) -> list[str]:
        self._record('portal_listen_ips', portal_id=portal_id)
        listen = self.portal['listen']
        return [entry['ip'] for entry in listen]

    def available_bytes(self, pool: str) -> int:
        self._record('available_bytes', pool=pool)
        return int(self.pool['free'])

    # -- iSCSI extent ------------------------------------------------------

    def find_extent_by_disk(self, zvol_path: str) -> JsonDict | None:
        self._record('find_extent_by_disk', zvol_path=zvol_path)
        for extent in self.extents.values():
            if extent['disk'] == zvol_path:
                return dict(extent)
        return None

    def create_extent(
        self,
        name: str,
        zvol_path: str,
        *,
        blocksize: int = common.EXTENT_BLOCKSIZE,
    ) -> JsonDict:
        self._record('create_extent', name=name, zvol_path=zvol_path)
        extent_id = self._next_id('extent')
        self.extents[extent_id] = {
            'id': extent_id,
            'name': name,
            'disk': zvol_path,
        }
        return dict(self.extents[extent_id])

    def delete_extent(
        self,
        extent_id: int,
        *,
        remove: bool = False,
        force: bool = True,
    ) -> None:
        self._record('delete_extent', extent_id=extent_id)
        self.extents.pop(extent_id, None)

    # -- iSCSI target ------------------------------------------------------

    def find_target_by_name(self, name: str) -> JsonDict | None:
        self._record('find_target_by_name', name=name)
        for target in self.targets.values():
            if target['name'] == name:
                return dict(target)
        return None

    def get_target(self, target_id: int) -> JsonDict | None:
        self._record('get_target', target_id=target_id)
        target = self.targets.get(target_id)
        return dict(target) if target is not None else None

    def create_target(
        self,
        name: str,
        alias: str,
        groups: list[JsonDict],
    ) -> JsonDict:
        self._record('create_target', name=name, alias=alias, groups=groups)
        target_id = self._next_id('target')
        self.targets[target_id] = {
            'id': target_id,
            'name': name,
            'alias': alias,
            'groups': groups,
        }
        return dict(self.targets[target_id])

    def update_target(self, target_id: int, updates: JsonDict) -> JsonDict:
        self._record('update_target', target_id=target_id, updates=updates)
        self.targets[target_id].update(updates)
        return dict(self.targets[target_id])

    def delete_target(
        self,
        target_id: int,
        *,
        force: bool = True,
        delete_extents: bool = False,
    ) -> None:
        self._record('delete_target', target_id=target_id)
        self.targets.pop(target_id, None)

    # -- iSCSI target-extent ----------------------------------------------

    def find_targetextent(
        self,
        *,
        target_id: int | None = None,
        extent_id: int | None = None,
    ) -> JsonDict | None:
        self._record(
            'find_targetextent', target_id=target_id, extent_id=extent_id
        )
        for te in self.targetextents.values():
            if target_id is not None and te['target'] != target_id:
                continue
            if extent_id is not None and te['extent'] != extent_id:
                continue
            return dict(te)
        return None

    def list_targetextents_for_target(self, target_id: int) -> list[JsonDict]:
        self._record('list_targetextents_for_target', target_id=target_id)
        return [
            dict(te)
            for te in self.targetextents.values()
            if te['target'] == target_id
        ]

    def create_targetextent(
        self,
        target_id: int,
        extent_id: int,
        *,
        lunid: int = common.LUN_ID,
    ) -> JsonDict:
        self._record(
            'create_targetextent', target_id=target_id, extent_id=extent_id
        )
        te_id = self._next_id('targetextent')
        self.targetextents[te_id] = {
            'id': te_id,
            'target': target_id,
            'extent': extent_id,
            'lunid': lunid,
        }
        return dict(self.targetextents[te_id])

    def delete_targetextent(self, te_id: int, *, force: bool = True) -> None:
        self._record('delete_targetextent', te_id=te_id)
        self.targetextents.pop(te_id, None)

    # -- iSCSI CHAP auth ---------------------------------------------------

    def next_auth_tag(self) -> int:
        self._record('next_auth_tag')
        tags = [a['tag'] for a in self.auths.values()]
        return (max(tags) + 1) if tags else 1

    def find_auth_by_tag(self, tag: int) -> JsonDict | None:
        self._record('find_auth_by_tag', tag=tag)
        for auth in self.auths.values():
            if auth['tag'] == tag:
                return dict(auth)
        return None

    def find_auth_by_user(self, user: str) -> JsonDict | None:
        self._record('find_auth_by_user', user=user)
        for auth in self.auths.values():
            if auth['user'] == user:
                return dict(auth)
        return None

    def create_auth(
        self,
        tag: int,
        user: str,
        secret: str,
        *,
        peeruser: str | None = None,
        peersecret: str | None = None,
    ) -> JsonDict:
        self._record('create_auth', tag=tag, user=user, secret=secret)
        auth_id = self._next_id('auth')
        self.auths[auth_id] = {
            'id': auth_id,
            'tag': tag,
            'user': user,
            'secret': secret,
        }
        return dict(self.auths[auth_id])

    def delete_auth(self, auth_id: int) -> None:
        self._record('delete_auth', auth_id=auth_id)
        self.auths.pop(auth_id, None)

    # -- iSCSI initiator ---------------------------------------------------

    def find_initiator_by_comment(self, comment: str) -> JsonDict | None:
        self._record('find_initiator_by_comment', comment=comment)
        for initiator in self.initiators.values():
            if initiator['comment'] == comment:
                return dict(initiator)
        return None

    def get_initiator(self, initiator_id: int) -> JsonDict | None:
        self._record('get_initiator', initiator_id=initiator_id)
        initiator = self.initiators.get(initiator_id)
        return dict(initiator) if initiator is not None else None

    def create_initiator(
        self, initiators: list[str], comment: str
    ) -> JsonDict:
        self._record(
            'create_initiator', initiators=list(initiators), comment=comment
        )
        initiator_id = self._next_id('initiator')
        self.initiators[initiator_id] = {
            'id': initiator_id,
            'initiators': list(initiators),
            'comment': comment,
        }
        return dict(self.initiators[initiator_id])

    def update_initiator(
        self, initiator_id: int, initiators: list[str]
    ) -> JsonDict:
        self._record(
            'update_initiator',
            initiator_id=initiator_id,
            initiators=list(initiators),
        )
        self.initiators[initiator_id]['initiators'] = list(initiators)
        return dict(self.initiators[initiator_id])

    def delete_initiator(self, initiator_id: int) -> None:
        self._record('delete_initiator', initiator_id=initiator_id)
        self.initiators.pop(initiator_id, None)
