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
"""Unit tests for the TrueNAS iSCSI driver.

The fake client models ZFS clone-dependency semantics, so the clone/snapshot
lifecycle assertions here exercise the same failure modes real hardware
would (dependent-clone blocking, defer, promote-on-delete).
"""

import tempfile
import unittest
from typing import Any, cast

from oslo_config import cfg

from cinder import coordination, exception
from tests.unit import fakes
from truenas_cinder import client as tn_client
from truenas_cinder import common
from truenas_cinder.driver import TrueNASISCSIDriver

CONF = cfg.CONF
IQN_BASE = 'iqn.2011-08.org.truenas.ctl'


class _DriverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        lock_dir = tempfile.mkdtemp()
        CONF.set_override(
            'backend_url', 'file://' + lock_dir, group='coordination'
        )
        coordination.COORDINATOR.start()
        self.addCleanup(coordination.COORDINATOR.stop)

        self.client = fakes.FakeTrueNASClient()
        self.driver = self._make_driver()

    def _make_driver(self, use_chap: bool = False) -> TrueNASISCSIDriver:
        driver = TrueNASISCSIDriver(
            configuration=fakes.FakeConfig(
                reserved_percentage=0, max_over_subscription_ratio=20.0
            ),
            host='host@truenas',
        )
        driver._client = cast(tn_client.TrueNASClient, self.client)
        driver._pool = 'tank'
        driver._dataset_root = 'cinder'
        driver._portal_id = 1
        driver._iqn_base = IQN_BASE
        driver._host_address = '10.0.0.1'
        driver._portal_ips = ['10.0.0.1']
        driver._use_chap = use_chap
        driver._sparse = True
        driver._volblocksize = '16K'
        driver._compression = 'LZ4'
        driver._backend_name = 'TrueNAS_iSCSI'
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
        self.driver._sparse = False
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
        self.driver._sparse = False
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
        # Snapshot is hidden but survives as the clone's origin.
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

        # Delete S: it has a clone, so it is deferred (hidden) but survives.
        self.driver.delete_snapshot(snap)
        snap_id = common.snapshot_id(
            self._dataset('vol'), common.snapshot_name('S')
        )
        self.assertIsNone(self.client.get_snapshot(snap_id))
        self.assertIn(self._dataset('V'), self.client.datasets)

        # Now tear everything down.
        self.driver.delete_volume(child)
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
        self.driver._portal_ips = ['10.0.0.1', '10.0.0.2']
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

    def test_terminate_connection_removes_host(self) -> None:
        vol = self._make_exported_volume('vol1')
        self.driver.initialize_connection(vol, {'initiator': 'iqn.host:a'})
        self.driver.initialize_connection(vol, {'initiator': 'iqn.host:b'})
        self.driver.terminate_connection(vol, {'initiator': 'iqn.host:a'})
        group = self.client.find_initiator_by_comment(
            common.target_name('vol1')
        )
        assert group is not None
        self.assertEqual(group['initiators'], ['iqn.host:b'])

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
        self.assertEqual(stats['driver_version'], TrueNASISCSIDriver.VERSION)
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

    def test_check_for_setup_error_ok(self) -> None:
        self.driver.check_for_setup_error()

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

    def test_check_for_setup_error_no_iqn_base(self) -> None:
        self.driver._iqn_base = ''
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.check_for_setup_error,
        )


if __name__ == '__main__':
    unittest.main()
