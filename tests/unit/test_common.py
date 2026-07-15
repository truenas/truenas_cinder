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
"""Unit tests for truenas_cinder.common."""

import unittest

from truenas_cinder import common


class UnitConversionTest(unittest.TestCase):
    def test_gib_to_bytes(self) -> None:
        self.assertEqual(common.gib_to_bytes(5), 5 * common.GIGABYTE)

    def test_gib_to_bytes_enforces_minimum(self) -> None:
        # A sub-1-GiB request is bumped to the 1 GiB floor.
        self.assertEqual(common.gib_to_bytes(0), common.GIGABYTE)

    def test_bytes_to_gib(self) -> None:
        self.assertEqual(common.bytes_to_gib(2 * common.GIGABYTE), 2.0)


class NamingTest(unittest.TestCase):
    def test_volume_name(self) -> None:
        self.assertEqual(common.volume_name('abc'), 'volume-abc')

    def test_dataset_name_with_root(self) -> None:
        self.assertEqual(
            common.dataset_name('tank', 'cinder', 'abc'),
            'tank/cinder/volume-abc',
        )

    def test_dataset_name_without_root(self) -> None:
        self.assertEqual(
            common.dataset_name('tank', '', 'abc'), 'tank/volume-abc'
        )

    def test_dataset_name_nested_root(self) -> None:
        self.assertEqual(
            common.dataset_name('tank', 'a/b', 'abc'),
            'tank/a/b/volume-abc',
        )

    def test_snapshot_id(self) -> None:
        self.assertEqual(
            common.snapshot_id('tank/volume-abc', 'snapshot-1'),
            'tank/volume-abc@snapshot-1',
        )

    def test_zvol_device_path(self) -> None:
        self.assertEqual(
            common.zvol_device_path('tank/volume-abc'),
            'zvol/tank/volume-abc',
        )

    def test_target_name_sanitized_and_truncated(self) -> None:
        name = common.target_name('AB_CD' + 'x' * 200)
        self.assertLessEqual(len(name), common.TARGET_NAME_MAXLEN)
        # Upper-cased and illegal chars ('_') are replaced.
        self.assertNotIn('_', name)
        self.assertEqual(name, name.lower())

    def test_extent_name_truncated(self) -> None:
        name = common.extent_name('x' * 200)
        self.assertLessEqual(len(name), common.EXTENT_NAME_MAXLEN)

    def test_target_iqn(self) -> None:
        self.assertEqual(
            common.target_iqn('iqn.2011-08.org.truenas.ctl', 'volume-abc'),
            'iqn.2011-08.org.truenas.ctl:volume-abc',
        )


if __name__ == '__main__':
    unittest.main()
