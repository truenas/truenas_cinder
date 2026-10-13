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
"""Unit tests for the TrueNAS Cinder driver.

The fake client models ZFS clone-dependency semantics, so the clone/snapshot
lifecycle assertions here exercise the same failure modes real hardware
would (dependent-clone blocking, defer, promote-on-delete).
"""

import tempfile
import unittest
from typing import Any, cast
from unittest import mock

from oslo_config import cfg

from cinder import coordination, exception
from tests.unit import fakes
from truenas_cinder import client as tn_client
from truenas_cinder import common
from truenas_cinder.driver import TrueNASDriver

CONF = cfg.CONF
IQN_BASE = fakes.TRUENAS_BASENAME


class _DriverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        lock_dir = tempfile.mkdtemp()
        CONF.set_override(
            'backend_url', 'file://' + lock_dir, group='coordination'
        )
        coordination.COORDINATOR.start()
        self.addCleanup(coordination.COORDINATOR.stop)

        # The fake client stands in for truenas_api_client, which cinder's
        # own test run does not install; without this, do_setup would stop
        # at the missing dependency.
        patcher = mock.patch.object(tn_client, 'DEPENDENCY_AVAILABLE', True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = fakes.FakeTrueNASClient()
        self.driver = self._make_driver()

    def _make_driver(
        self, use_chap: bool = False, **options: Any
    ) -> TrueNASDriver:
        """A driver configured as cinder.conf would, run through do_setup.

        Going through the real do_setup (rather than assigning private
        attributes) is what exercises option wiring, the IQN base lookup,
        and portal resolution.
        """
        config: dict[str, Any] = {
            'truenas_ip': '10.0.0.1',
            'truenas_api_key': '16-testkey',
            'truenas_pool': 'tank',
            'use_chap_auth': use_chap,
            'volume_backend_name': 'TrueNAS',
            'reserved_percentage': 0,
            'max_over_subscription_ratio': 20.0,
        }
        config.update(options)
        driver = TrueNASDriver(
            configuration=fakes.FakeConfig(**config), host='host@truenas'
        )
        driver._client = cast(tn_client.TrueNASClient, self.client)
        driver.do_setup(None)
        return driver

    def _dataset(self, name_id: str) -> str:
        return common.dataset_name('tank', 'cinder', name_id)


class VolumeOpsTest(_DriverTestCase):
    def test_create_volume(self) -> None:
        vol = fakes.FakeVolume('vol1', size=3)
        model = self.driver.create_volume(vol)
        dataset = self._dataset('vol1')
        self.assertEqual(model['provider_location'], dataset)
        self.assertIn(dataset, self.client.datasets)
        self.assertEqual(
            self.client.datasets[dataset]['volsize'], 3 * common.GIGABYTE
        )
        # Sparse (thin) means no refreservation.
        self.assertEqual(self.client.datasets[dataset]['refreservation'], 0)

    def test_create_volume_thick_sets_refreservation(self) -> None:
        self.driver = self._make_driver(truenas_sparse=False)
        vol = fakes.FakeVolume('vol1', size=2)
        self.driver.create_volume(vol)
        dataset = self._dataset('vol1')
        self.assertEqual(
            self.client.datasets[dataset]['refreservation'],
            2 * common.GIGABYTE,
        )

    def test_delete_volume(self) -> None:
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        self.driver.delete_volume(vol)
        self.assertNotIn(self._dataset('vol1'), self.client.datasets)

    def test_delete_volume_not_found_is_success(self) -> None:
        vol = fakes.FakeVolume('ghost')
        # No exception even though the dataset never existed.
        self.driver.delete_volume(vol)

    def test_delete_is_never_recursive(self) -> None:
        # On TrueNAS 27 a recursive delete also destroys clones -- other
        # volumes -- so the driver must never request one.
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        self.driver.delete_volume(vol)
        deletes = [
            kw for name, kw in self.client.calls if name == 'delete_dataset'
        ]
        self.assertEqual(len(deletes), 1)
        self.assertFalse(deletes[0]['recursive'])

    def test_delete_removes_own_snapshots_first(self) -> None:
        # A non-recursive delete is refused while snapshots exist, so the
        # volume's own snapshots go first, one at a time.
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        dataset = self._dataset('vol1')
        self.client.create_snapshot(dataset, 'leftover')
        self.driver.delete_volume(vol)

        names = self.client.call_names()
        self.assertLess(
            names.index('delete_snapshot'), names.index('delete_dataset')
        )
        snapshot_delete = next(
            kw for name, kw in self.client.calls if name == 'delete_snapshot'
        )
        self.assertEqual(
            snapshot_delete['snapshot_id'],
            common.snapshot_id(dataset, 'leftover'),
        )
        self.assertFalse(snapshot_delete['defer'])
        self.assertNotIn(dataset, self.client.datasets)

    def test_extend_volume_thin(self) -> None:
        vol = fakes.FakeVolume('vol1', size=1)
        self.driver.create_volume(vol)
        self.driver.extend_volume(vol, 10)
        dataset = self._dataset('vol1')
        self.assertEqual(
            self.client.datasets[dataset]['volsize'], 10 * common.GIGABYTE
        )
        self.assertEqual(self.client.datasets[dataset]['refreservation'], 0)

    def test_extend_volume_thick_updates_refreservation(self) -> None:
        self.driver = self._make_driver(truenas_sparse=False)
        vol = fakes.FakeVolume('vol1', size=1)
        self.driver.create_volume(vol)
        self.driver.extend_volume(vol, 5)
        dataset = self._dataset('vol1')
        self.assertEqual(
            self.client.datasets[dataset]['refreservation'],
            5 * common.GIGABYTE,
        )


class SnapshotTest(_DriverTestCase):
    def _prep_volume(self, name_id: str = 'vol1') -> fakes.FakeVolume:
        vol = fakes.FakeVolume(name_id)
        self.driver.create_volume(vol)
        return vol

    def test_create_snapshot(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        snap_id = common.snapshot_id(
            self._dataset('vol1'), common.snapshot_name('snap1')
        )
        self.assertIn(snap_id, self.client.snapshots)

    def test_create_snapshot_idempotent(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        # Second call must not raise (would if it re-created on the backend).
        self.driver.create_snapshot(snap)

    def test_delete_snapshot_without_clones_uses_defer_false(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        self.driver.delete_snapshot(snap)
        delete_calls = [
            kw for name, kw in self.client.calls if name == 'delete_snapshot'
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertFalse(delete_calls[0]['defer'])

    def test_delete_snapshot_with_clones_uses_defer_true(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol, volume_size=1)
        self.driver.create_snapshot(snap)
        # Create a dependent clone off the snapshot.
        clone_vol = fakes.FakeVolume('clone1', size=1)
        self.driver.create_volume_from_snapshot(clone_vol, snap)

        self.driver.delete_snapshot(snap)
        delete_calls = [
            kw for name, kw in self.client.calls if name == 'delete_snapshot'
        ]
        self.assertTrue(delete_calls[-1]['defer'])
        # The snapshot survives as the clone's origin, pending release.
        snap_id = common.snapshot_id(
            self._dataset('vol1'), common.snapshot_name('snap1')
        )
        self.assertIn(snap_id, self.client.snapshots)
        self.assertTrue(self.client.snapshots[snap_id]['deferred'])

    def test_revert_to_snapshot(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        self.driver.revert_to_snapshot(None, vol, snap)
        self.assertIn('rollback_snapshot', self.client.call_names())

    def test_revert_to_snapshot_blocked_by_dependent_clone(self) -> None:
        # Cloning leaves a hidden origin snapshot newer than snap1, which
        # ZFS will not roll back past. Fail with an explanation instead of
        # surfacing a raw ZFS error.
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        clone = fakes.FakeVolume('clone1', size=1)
        self.driver.create_cloned_volume(clone, vol)

        with self.assertRaises(exception.VolumeBackendAPIException) as ctx:
            self.driver.revert_to_snapshot(None, vol, snap)
        self.assertIn('dependent clones', str(ctx.exception))
        # No rollback was attempted.
        self.assertNotIn('rollback_snapshot', self.client.call_names())

    def test_revert_allowed_after_clone_is_deleted(self) -> None:
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        clone = fakes.FakeVolume('clone1', size=1)
        self.driver.create_cloned_volume(clone, vol)
        self.driver.delete_volume(clone)

        self.driver.revert_to_snapshot(None, vol, snap)
        self.assertIn('rollback_snapshot', self.client.call_names())

    def test_revert_opts_out_of_cinders_temporary_snapshot(self) -> None:
        # Unless the driver opts out, Cinder snapshots the volume right
        # before reverting it. That snapshot is newer than the target, and
        # ZFS will not roll back past it.
        self.assertFalse(self.driver.snapshot_revert_use_temp_snapshot())
        vol = self._prep_volume()
        snap = fakes.FakeSnapshot('snap1', vol)
        self.driver.create_snapshot(snap)
        self.driver.create_snapshot(fakes.FakeSnapshot('revert-backup', vol))
        with self.assertRaises(fakes.TrueNASApiError):
            self.driver.revert_to_snapshot(None, vol, snap)


class RetypeTest(_DriverTestCase):
    def test_retype_succeeds_without_touching_truenas(self) -> None:
        # e.g. to a multiattach type: every volume already supports it.
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        self.client.calls.clear()
        diff = {'extra_specs': {'multiattach': (None, '<is> True')}}
        # Cinder's default returns (False, None), which is truthy.
        self.assertIs(
            self.driver.retype(None, vol, {'id': 'type2'}, diff, {}), True
        )
        self.assertEqual(self.client.calls, [])


class CloneLifecycleTest(_DriverTestCase):
    """The correction #1 scenarios: clone + defer + promote, no leaks."""

    def _prep_volume(self, name_id: str, size: int = 1) -> fakes.FakeVolume:
        vol = fakes.FakeVolume(name_id, size=size)
        self.driver.create_volume(vol)
        return vol

    def test_create_volume_from_snapshot_keeps_source_snapshot(self) -> None:
        src = self._prep_volume('src', size=1)
        snap = fakes.FakeSnapshot('snap1', src, volume_size=1)
        self.driver.create_snapshot(snap)

        dst = fakes.FakeVolume('dst', size=1)
        model = self.driver.create_volume_from_snapshot(dst, snap)

        dst_dataset = self._dataset('dst')
        self.assertEqual(model['provider_location'], dst_dataset)
        self.assertIn(dst_dataset, self.client.datasets)
        # The source snapshot is a first-class Cinder object -- never deleted.
        self.assertNotIn(
            'delete_snapshot',
            [name for name, _ in self.client.calls],
        )

    def test_create_volume_from_snapshot_resizes_when_larger(self) -> None:
        src = self._prep_volume('src', size=1)
        snap = fakes.FakeSnapshot('snap1', src, volume_size=1)
        self.driver.create_snapshot(snap)
        dst = fakes.FakeVolume('dst', size=8)
        self.driver.create_volume_from_snapshot(dst, snap)
        self.assertEqual(
            self.client.datasets[self._dataset('dst')]['volsize'],
            8 * common.GIGABYTE,
        )

    def test_create_cloned_volume_call_sequence(self) -> None:
        src = self._prep_volume('src', size=1)
        dst = fakes.FakeVolume('dst', size=1)
        self.driver.create_cloned_volume(dst, src)

        seq = [
            name
            for name, _ in self.client.calls
            if name
            in {
                'create_snapshot',
                'clone_snapshot',
                'delete_snapshot',
            }
        ]
        self.assertEqual(
            seq, ['create_snapshot', 'clone_snapshot', 'delete_snapshot']
        )
        # The intermediate snapshot was hidden with defer=True.
        defer_call = [
            kw for name, kw in self.client.calls if name == 'delete_snapshot'
        ][0]
        self.assertTrue(defer_call['defer'])
        self.assertIn(self._dataset('dst'), self.client.datasets)

    def test_create_cloned_volume_reverse_compensates_on_clone_fail(
        self,
    ) -> None:
        src = self._prep_volume('src', size=1)
        dst = fakes.FakeVolume('dst', size=1)

        def boom(snapshot_id: str, dataset_dst: str) -> None:
            raise fakes.TrueNASApiError('clone failed')

        self.client.clone_snapshot = boom  # type: ignore[method-assign]
        self.assertRaises(
            fakes.TrueNASApiError,
            self.driver.create_cloned_volume,
            dst,
            src,
        )
        # Intermediate snapshot was cleaned up (defer=False); no clone leaked.
        self.assertNotIn(self._dataset('dst'), self.client.datasets)
        snap_id = common.snapshot_id(self._dataset('src'), 'clone-dst')
        self.assertNotIn(snap_id, self.client.snapshots)

    def test_create_cloned_volume_reverse_compensates_on_resize_fail(
        self,
    ) -> None:
        src = self._prep_volume('src', size=1)
        # Volume larger than source triggers a resize; make it fail.
        dst = fakes.FakeVolume('dst', size=5)

        def boom(path: str, updates: dict[str, Any]) -> dict[str, Any]:
            raise fakes.TrueNASApiError('resize failed')

        self.client.update_dataset = boom  # type: ignore[method-assign]
        self.assertRaises(
            fakes.TrueNASApiError,
            self.driver.create_cloned_volume,
            dst,
            src,
        )
        # Clone and intermediate snapshot both removed.
        self.assertNotIn(self._dataset('dst'), self.client.datasets)
        snap_id = common.snapshot_id(self._dataset('src'), 'clone-dst')
        self.assertNotIn(snap_id, self.client.snapshots)

    def test_create_cloned_volume_idempotent(self) -> None:
        src = self._prep_volume('src', size=1)
        dst = fakes.FakeVolume('dst', size=1)
        self.driver.create_cloned_volume(dst, src)
        snap_count = len(self.client.snapshots)
        clone_calls = self.client.call_names().count('clone_snapshot')

        # Re-run (simulating a mid-op WS drop + retry): must self-heal.
        self.driver.create_cloned_volume(dst, src)
        self.assertEqual(len(self.client.snapshots), snap_count)
        self.assertEqual(
            self.client.call_names().count('clone_snapshot'), clone_calls
        )

    def test_delete_source_promotes_before_delete(self) -> None:
        src = self._prep_volume('src', size=1)
        dst = fakes.FakeVolume('dst', size=1)
        self.driver.create_cloned_volume(dst, src)
        self.client.calls.clear()

        self.driver.delete_volume(src)

        names = self.client.call_names()
        self.assertIn('promote_dataset', names)
        self.assertIn('delete_dataset', names)
        self.assertLess(
            names.index('promote_dataset'), names.index('delete_dataset')
        )
        # Source gone; clone survives and is now independent.
        self.assertNotIn(self._dataset('src'), self.client.datasets)
        self.assertIn(self._dataset('dst'), self.client.datasets)
        self.assertIsNone(self.client.datasets[self._dataset('dst')]['origin'])

    def test_full_sequence_clone_then_delete_source(self) -> None:
        # Tempest-shaped: create A -> clone B -> delete A.
        vol_a = self._prep_volume('A', size=1)
        vol_b = fakes.FakeVolume('B', size=1)
        self.driver.create_cloned_volume(vol_b, vol_a)
        self.driver.delete_volume(vol_a)

        self.assertNotIn(self._dataset('A'), self.client.datasets)
        self.assertIn(self._dataset('B'), self.client.datasets)
        # No orphaned snapshots remain.
        self.assertEqual(self.client.snapshots, {})

    def test_full_sequence_from_snapshot_then_delete(self) -> None:
        # create vol -> snapshot S -> from-snapshot V -> delete S -> cleanup.
        vol = self._prep_volume('vol', size=1)
        snap = fakes.FakeSnapshot('S', vol, volume_size=1)
        self.driver.create_snapshot(snap)
        child = fakes.FakeVolume('V', size=1)
        self.driver.create_volume_from_snapshot(child, snap)

        # Delete S: it has a clone, so it is destroyed with defer=True. The
        # snapshot stays on the backend (defer_destroy) holding the clone's
        # origin, and is reaped when the clone goes away.
        self.driver.delete_snapshot(snap)
        snap_id = common.snapshot_id(
            self._dataset('vol'), common.snapshot_name('S')
        )
        row = self.client.get_snapshot(snap_id)
        assert row is not None
        self.assertTrue(row['properties']['defer_destroy']['parsed'])
        self.assertIn(self._dataset('V'), self.client.datasets)

        # Now tear everything down.
        self.driver.delete_volume(child)
        self.assertIsNone(self.client.get_snapshot(snap_id))
        self.driver.delete_volume(vol)
        self.assertEqual(self.client.datasets, {})
        self.assertEqual(self.client.snapshots, {})

    def test_multi_clone_chain(self) -> None:
        # One source with clones off two different snapshots.
        src = self._prep_volume('src', size=1)
        snap1 = fakes.FakeSnapshot('s1', src, volume_size=1)
        snap2 = fakes.FakeSnapshot('s2', src, volume_size=1)
        self.driver.create_snapshot(snap1)
        self.driver.create_snapshot(snap2)
        clone1 = fakes.FakeVolume('c1', size=1)
        clone2 = fakes.FakeVolume('c2', size=1)
        self.driver.create_volume_from_snapshot(clone1, snap1)
        self.driver.create_volume_from_snapshot(clone2, snap2)

        # Deleting the source must detach both lineages.
        self.driver.delete_volume(src)
        self.assertNotIn(self._dataset('src'), self.client.datasets)
        self.assertIn(self._dataset('c1'), self.client.datasets)
        self.assertIn(self._dataset('c2'), self.client.datasets)

    def test_delete_refuses_when_clones_cannot_be_detached(self) -> None:
        # If promotion cannot detach a clone, deleting the source must stop
        # with VolumeIsBusy (Cinder then keeps the volume) -- never delete
        # anyway. On TrueNAS 27 deleting anyway destroyed the clone.
        src = self._prep_volume('src', size=1)
        dst = fakes.FakeVolume('dst', size=1)
        self.driver.create_cloned_volume(dst, src)
        self.client.promote_dataset = mock.Mock()  # type: ignore[method-assign]

        with self.assertRaises(exception.VolumeIsBusy) as ctx:
            self.driver.delete_volume(src)
        self.assertIn(self._dataset('dst'), str(ctx.exception))
        # Both volumes are untouched and nothing was deleted.
        self.assertIn(self._dataset('src'), self.client.datasets)
        self.assertIn(self._dataset('dst'), self.client.datasets)
        self.assertNotIn('delete_dataset', self.client.call_names())


class CloneLifecycleOnTrueNAS27Test(CloneLifecycleTest):
    """The clone lifecycle again, with TrueNAS 27's delete semantics.

    On TrueNAS 27 a recursive delete is ``zfs destroy -R`` and takes clones
    with it, so a driver that deleted recursively would pass on 25.10 and
    destroy volumes on 27.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.recursive_destroys_clones = True


class FakeDeleteSemanticsTest(unittest.TestCase):
    """The fake's delete must model ZFS, or the lifecycle tests prove
    nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.client = fakes.FakeTrueNASClient()
        common_args: dict[str, Any] = {
            'volblocksize': '16K',
            'compression': 'LZ4',
            'sparse': True,
        }
        self.client.create_zvol('tank/src', common.GIGABYTE, **common_args)
        self.client.create_snapshot('tank/src', 's')
        self.client.clone_snapshot('tank/src@s', 'tank/clone')
        self.client.create_snapshot('tank/clone', 't')
        self.client.clone_snapshot('tank/clone@t', 'tank/grandclone')

    def test_non_recursive_delete_refuses_with_snapshots(self) -> None:
        with self.assertRaises(fakes.TrueNASApiError):
            self.client.delete_dataset('tank/src')
        self.assertIn('tank/src', self.client.datasets)

    def test_rollback_refuses_while_newer_snapshots_exist(self) -> None:
        self.client.create_snapshot('tank/src', 'newer')
        with self.assertRaises(fakes.TrueNASApiError):
            self.client.rollback_snapshot('tank/src@s')
        self.client.rollback_snapshot('tank/src@newer')

    def test_recursive_delete_on_25_10_refuses_clone_origin(self) -> None:
        with self.assertRaises(fakes.TrueNASApiError):
            self.client.delete_dataset('tank/src', recursive=True)
        self.assertIn('tank/clone', self.client.datasets)

    def test_recursive_delete_on_27_destroys_clones(self) -> None:
        self.client.recursive_destroys_clones = True
        self.client.delete_dataset('tank/src', recursive=True)
        # The clone and its own clone went with it.
        self.assertEqual(self.client.datasets, {})
        self.assertEqual(self.client.snapshots, {})


class ExportConnectionTest(_DriverTestCase):
    def _make_exported_volume(
        self, name_id: str = 'vol1', use_chap: bool = False
    ) -> fakes.FakeVolume:
        if use_chap:
            self.driver = self._make_driver(use_chap=True)
        vol = fakes.FakeVolume(name_id)
        self.driver.create_volume(vol)
        model = self.driver.create_export(None, vol, {})
        if model and 'provider_auth' in model:
            vol.provider_auth = model['provider_auth']
        if model and 'provider_location' in model:
            vol.provider_location = model['provider_location']
        return vol

    def test_create_export_builds_chain(self) -> None:
        self._make_exported_volume('vol1')
        tname = common.target_name('vol1')
        self.assertIsNotNone(self.client.find_target_by_name(tname))
        zvol = common.zvol_device_path(self._dataset('vol1'))
        self.assertIsNotNone(self.client.find_extent_by_disk(zvol))
        self.assertEqual(len(self.client.targetextents), 1)

    def test_create_export_no_chap_has_no_auth(self) -> None:
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        model = self.driver.create_export(None, vol, {})
        assert model is not None
        self.assertNotIn('provider_auth', model)
        self.assertEqual(len(self.client.auths), 0)

    def test_create_export_with_chap(self) -> None:
        self.driver = self._make_driver(use_chap=True)
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        model = self.driver.create_export(None, vol, {})
        assert model is not None
        self.assertIn('provider_auth', model)
        self.assertTrue(model['provider_auth'].startswith('CHAP '))
        self.assertEqual(len(self.client.auths), 1)
        # Target group references the CHAP tag.
        target = self.client.find_target_by_name(common.target_name('vol1'))
        assert target is not None
        group = target['groups'][0]
        self.assertEqual(group['authmethod'], 'CHAP')
        self.assertEqual(
            group['auth'], list(self.client.auths.values())[0]['tag']
        )

    def test_ensure_chain_idempotent(self) -> None:
        vol = fakes.FakeVolume('vol1')
        self.driver.create_volume(vol)
        self.driver.create_export(None, vol, {})
        self.driver.create_export(None, vol, {})
        self.assertEqual(len(self.client.targets), 1)
        self.assertEqual(len(self.client.extents), 1)
        self.assertEqual(len(self.client.targetextents), 1)

    def test_initialize_connection_single_path(self) -> None:
        vol = self._make_exported_volume('vol1')
        info = self.driver.initialize_connection(
            vol, {'initiator': 'iqn.host:a', 'multipath': False}
        )
        self.assertEqual(info['driver_volume_type'], 'iscsi')
        data = info['data']
        self.assertEqual(
            data['target_iqn'],
            common.target_iqn(IQN_BASE, common.target_name('vol1')),
        )
        self.assertEqual(data['target_portal'], '10.0.0.1:3260')
        self.assertEqual(data['target_lun'], 0)
        self.assertTrue(data['discard'])
        self.assertNotIn('target_iqns', data)

    def test_initialize_connection_multipath_flag(self) -> None:
        vol = self._make_exported_volume('vol1')
        info = self.driver.initialize_connection(
            vol, {'initiator': 'iqn.host:a', 'multipath': True}
        )
        data = info['data']
        self.assertEqual(data['target_iqns'], [data['target_iqn']])
        self.assertEqual(data['target_portals'], ['10.0.0.1:3260'])
        self.assertEqual(data['target_luns'], [0])

    def test_initialize_connection_multiple_portals(self) -> None:
        self.client.portal['listen'] = [
            {'ip': '10.0.0.1', 'port': 3260},
            {'ip': '10.0.0.2', 'port': 3260},
        ]
        self.driver = self._make_driver()
        vol = self._make_exported_volume('vol1')
        info = self.driver.initialize_connection(
            vol, {'initiator': 'iqn.host:a'}
        )
        data = info['data']
        self.assertEqual(
            data['target_portals'], ['10.0.0.1:3260', '10.0.0.2:3260']
        )
        self.assertEqual(len(data['target_iqns']), 2)

    def test_initialize_connection_multiattach_adds_initiators(self) -> None:
        vol = self._make_exported_volume('vol1')
        self.driver.initialize_connection(vol, {'initiator': 'iqn.host:a'})
        self.driver.initialize_connection(vol, {'initiator': 'iqn.host:b'})
        group = self.client.find_initiator_by_comment(
            common.target_name('vol1')
        )
        assert group is not None
        self.assertEqual(
            sorted(group['initiators']), ['iqn.host:a', 'iqn.host:b']
        )

    def test_initialize_connection_chap_fields(self) -> None:
        vol = self._make_exported_volume('vol1', use_chap=True)
        info = self.driver.initialize_connection(
            vol, {'initiator': 'iqn.host:a'}
        )
        data = info['data']
        self.assertEqual(data['auth_method'], 'CHAP')
        self.assertIn('auth_username', data)
        self.assertIn('auth_password', data)

    def _attach(self, vol: fakes.FakeVolume, host: str) -> dict[str, str]:
        connector = {'initiator': f'iqn.{host}', 'host': host}
        self.driver.initialize_connection(vol, connector)
        vol.volume_attachment.append(fakes.FakeAttachment(host))
        return connector

    def _detach(
        self, vol: fakes.FakeVolume, connector: dict[str, str]
    ) -> None:
        # As Cinder's attachment_delete: the attachment is still listed
        # during terminate_connection, and remove_export follows unless the
        # driver reports the connection as shared.
        shared = self.driver.terminate_connection(vol, connector)
        if not shared:
            self.driver.remove_export(None, vol)
        for attachment in vol.volume_attachment:
            if attachment.attached_host == connector['host']:
                vol.volume_attachment.remove(attachment)
                break

    def _allowed_initiators(self, name_id: str = 'vol1') -> list[str]:
        group = self.client.find_initiator_by_comment(
            common.target_name(name_id)
        )
        assert group is not None
        return sorted(group['initiators'])

    def test_detach_from_one_host_keeps_target_for_the_other(self) -> None:
        vol = self._make_exported_volume('vol1')
        host_a = self._attach(vol, 'host-a')
        self._attach(vol, 'host-b')
        self._detach(vol, host_a)
        self.assertEqual(self._allowed_initiators(), ['iqn.host-b'])
        self.assertEqual(len(self.client.targets), 1)

    def test_detach_one_of_two_instances_on_a_host_keeps_access(self) -> None:
        # A multiattach volume used by two instances on one compute host:
        # both share that host's initiator and iSCSI session.
        vol = self._make_exported_volume('vol1')
        first = self._attach(vol, 'host-a')
        self._attach(vol, 'host-a')
        self._detach(vol, first)
        self.assertEqual(self._allowed_initiators(), ['iqn.host-a'])
        self.assertEqual(len(self.client.targets), 1)

    def test_detach_last_attachment_removes_target(self) -> None:
        vol = self._make_exported_volume('vol1')
        connector = self._attach(vol, 'host-a')
        self.assertFalse(self.driver.terminate_connection(vol, connector))
        self.assertEqual(self._allowed_initiators(), [])

    def test_terminate_connection_none_clears_all(self) -> None:
        vol = self._make_exported_volume('vol1')
        self.driver.initialize_connection(vol, {'initiator': 'iqn.host:a'})
        self.driver.terminate_connection(vol, None)
        group = self.client.find_initiator_by_comment(
            common.target_name('vol1')
        )
        assert group is not None
        self.assertEqual(group['initiators'], [])

    def test_remove_export_tears_down_chain(self) -> None:
        vol = self._make_exported_volume('vol1')
        self.driver.remove_export(None, vol)
        self.assertEqual(len(self.client.targets), 0)
        self.assertEqual(len(self.client.extents), 0)
        self.assertEqual(len(self.client.targetextents), 0)
        # The zvol itself is left intact.
        self.assertIn(self._dataset('vol1'), self.client.datasets)


class StatsAndSetupTest(_DriverTestCase):
    def test_get_volume_stats(self) -> None:
        stats = self.driver.get_volume_stats(refresh=True)
        self.assertEqual(stats['storage_protocol'], 'iSCSI')
        self.assertEqual(stats['vendor_name'], 'TrueNAS')
        self.assertEqual(stats['driver_version'], TrueNASDriver.VERSION)
        pool = stats['pools'][0]
        for key in (
            'total_capacity_gb',
            'free_capacity_gb',
            'provisioned_capacity_gb',
            'thin_provisioning_support',
            'thick_provisioning_support',
            'max_over_subscription_ratio',
            'reserved_percentage',
        ):
            self.assertIn(key, pool)
        self.assertTrue(pool['multiattach'])
        self.assertFalse(pool['QoS_support'])

    def test_capacity_is_usable_space_not_raw_pool_size(self) -> None:
        # pool.query's size (1000 GiB in the fake) is the raw vdev size; the
        # root dataset's used + available is what volumes can actually use.
        pool = self.driver.get_volume_stats(refresh=True)['pools'][0]
        self.assertEqual(pool['total_capacity_gb'], 950)
        self.assertEqual(pool['free_capacity_gb'], 850)

    def test_provisioned_capacity_is_sum_of_volume_sizes(self) -> None:
        # Thin volumes use almost no pool space, but the scheduler's
        # over-subscription check needs the sizes promised to them.
        self.client.pool['allocated'] = 1 * common.GIGABYTE
        self.driver.create_volume(fakes.FakeVolume('vol1', size=10))
        self.driver.create_volume(fakes.FakeVolume('vol2', size=5))
        pool = self.driver.get_volume_stats(refresh=True)['pools'][0]
        self.assertEqual(pool['provisioned_capacity_gb'], 15)

    def test_provisioned_capacity_counts_clones_at_full_size(self) -> None:
        self.driver.create_volume(fakes.FakeVolume('src', size=4))
        self.driver.create_cloned_volume(
            fakes.FakeVolume('clone', size=6), fakes.FakeVolume('src', size=4)
        )
        pool = self.driver.get_volume_stats(refresh=True)['pools'][0]
        self.assertEqual(pool['provisioned_capacity_gb'], 10)

    def test_provisioned_capacity_includes_zvols_cinder_did_not_create(
        self,
    ) -> None:
        # Cinder defines provisioned capacity as everything provisioned on
        # the backend, not only the volumes it manages.
        self.client.datasets['tank/vms/disk0'] = {
            'volsize': 8 * common.GIGABYTE,
            'refreservation': 0,
            'origin': None,
        }
        self.driver.create_volume(fakes.FakeVolume('vol1', size=2))
        pool = self.driver.get_volume_stats(refresh=True)['pools'][0]
        self.assertEqual(pool['provisioned_capacity_gb'], 10)

    def test_check_for_setup_error_ok(self) -> None:
        self.driver.check_for_setup_error()

    def test_check_for_setup_error_reports_import_error(self) -> None:
        # The real reason must reach the log, not just "not installed".
        with (
            mock.patch.object(tn_client, 'DEPENDENCY_AVAILABLE', False),
            mock.patch.object(
                tn_client, '_import_error', "No module named 'websocket'"
            ),
        ):
            with self.assertRaises(exception.VolumeBackendAPIException) as ctx:
                self.driver.check_for_setup_error()
        self.assertIn("No module named 'websocket'", str(ctx.exception))

    def test_check_for_setup_error_no_pool(self) -> None:
        self.driver._pool = ''
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.check_for_setup_error,
        )

    def test_check_for_setup_error_missing_pool_on_backend(self) -> None:
        self.driver._pool = 'nonexistent'
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.check_for_setup_error,
        )

    def test_check_for_setup_error_no_basename(self) -> None:
        self.client.iscsi_global['basename'] = ''
        self.driver = self._make_driver()
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.check_for_setup_error,
        )


class SetupTest(_DriverTestCase):
    """do_setup: option wiring, IQN base, and portal resolution."""

    def _attach_data(self, driver: TrueNASDriver) -> dict[str, Any]:
        vol = fakes.FakeVolume('vol1')
        driver.create_volume(vol)
        driver.create_export(None, vol, {})
        info = driver.initialize_connection(
            vol, {'initiator': 'iqn.host:a', 'multipath': True}
        )
        return info['data']

    def _connect_call(self) -> dict[str, Any]:
        calls = [kw for name, kw in self.client.calls if name == 'connect']
        self.assertTrue(calls, 'do_setup never connected')
        return calls[-1]

    def test_iqn_comes_from_truenas_basename(self) -> None:
        data = self._attach_data(self.driver)
        self.assertEqual(
            data['target_iqn'],
            f'{fakes.TRUENAS_BASENAME}:{common.target_name("vol1")}',
        )

    def test_cinder_target_prefix_is_ignored(self) -> None:
        # Regression: Cinder always supplies a target_prefix default
        # ('iqn.2010-10.org.openstack:'). Using it produced
        # 'iqn.2010-10.org.openstack::volume-...' -- a wrong IQN with a
        # double colon, which only failed later at iSCSI login.
        driver = self._make_driver(target_prefix='iqn.2010-10.org.openstack:')
        iqn = self._attach_data(driver)['target_iqn']
        self.assertTrue(iqn.startswith(f'{fakes.TRUENAS_BASENAME}:'), iqn)
        self.assertNotIn('openstack', iqn)
        self.assertNotIn('::', iqn)

    def test_no_driver_option_for_target_prefix(self) -> None:
        names = [opt.name for opt in TrueNASDriver.get_driver_options()]
        self.assertNotIn('target_prefix', names)

    def test_portal_port_comes_from_listen_entry(self) -> None:
        self.client.portal['listen'] = [{'ip': '10.0.0.1', 'port': 3261}]
        driver = self._make_driver()
        self.assertEqual(driver._portals, ['10.0.0.1:3261'])

    def test_portal_port_falls_back_to_global_port(self) -> None:
        self.client.portal['listen'] = [{'ip': '10.0.0.1'}]
        self.client.iscsi_global['listen_port'] = 3262
        driver = self._make_driver()
        self.assertEqual(driver._portals, ['10.0.0.1:3262'])

    def test_wildcard_portal_resolves_to_truenas_ip(self) -> None:
        self.client.portal['listen'] = [{'ip': '0.0.0.0', 'port': 3260}]
        driver = self._make_driver(truenas_ip='192.0.2.10')
        self.assertEqual(driver._portals, ['192.0.2.10:3260'])

    def test_ipv6_portal_is_bracketed(self) -> None:
        self.client.portal['listen'] = [{'ip': 'fd00::5', 'port': 3260}]
        driver = self._make_driver()
        self.assertEqual(driver._portals, ['[fd00::5]:3260'])

    def test_duplicate_portals_are_collapsed(self) -> None:
        # A wildcard bind plus the same address explicitly would otherwise
        # hand multipathd two identical paths.
        self.client.portal['listen'] = [
            {'ip': '0.0.0.0', 'port': 3260},
            {'ip': '10.0.0.1', 'port': 3260},
        ]
        driver = self._make_driver()
        self.assertEqual(driver._portals, ['10.0.0.1:3260'])

    def test_connection_options_are_wired_through(self) -> None:
        call = self._connect_call()
        self.assertEqual(call['url'], 'wss://10.0.0.1/api/current')
        self.assertEqual(call['username'], 'truenas_admin')
        self.assertEqual(call['api_key'], '16-testkey')
        self.assertEqual(call['auth_mechanism'], 'PLAIN')
        self.assertTrue(call['verify_ssl'])

    def test_connection_option_overrides(self) -> None:
        self._make_driver(
            truenas_login='cinder',
            truenas_auth_mechanism='SCRAM',
            truenas_verify_ssl=False,
            truenas_api_url='wss://truenas.example.com/api/current',
        )
        call = self._connect_call()
        self.assertEqual(call['url'], 'wss://truenas.example.com/api/current')
        self.assertEqual(call['username'], 'cinder')
        self.assertEqual(call['auth_mechanism'], 'SCRAM')
        self.assertFalse(call['verify_ssl'])

    def test_api_version_mismatch_only_warns(self) -> None:
        self.client.advertised_versions = ['v25.04.0']
        with self.assertLogs('truenas_cinder.driver', 'WARNING') as logs:
            driver = self._make_driver()
        self.assertIn('v25.10.0', logs.output[0])
        self.assertEqual(driver._iqn_base, fakes.TRUENAS_BASENAME)


if __name__ == '__main__':
    unittest.main()
