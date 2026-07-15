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
"""Naming, unit conversion, and sanitization helpers.

Kept dependency-free (stdlib only) so the module ports cleanly in-tree and is
trivial to unit test in isolation.
"""

import re

# --- ZFS / iSCSI constants ------------------------------------------------

#: One gibibyte in bytes. zvol sizes are handled in bytes on the TrueNAS
#: side; Cinder expresses sizes in whole GiB.
GIGABYTE: int = 1024**3

#: TrueNAS rejects zvols smaller than 1 GiB; bump any smaller request up.
MIN_VOLUME_BYTES: int = GIGABYTE

#: iSCSI CHAP secrets must be 12-16 characters (enforced by TrueNAS).
CHAP_SECRET_MIN_LEN: int = 12
CHAP_SECRET_MAX_LEN: int = 16

#: Every volume maps to exactly one target/extent at LUN 0 (matches CSI).
LUN_ID: int = 0

#: iSCSI extent logical block size, in bytes.
EXTENT_BLOCKSIZE: int = 512

#: TrueNAS object-name length ceilings.
TARGET_NAME_MAXLEN: int = 120
EXTENT_NAME_MAXLEN: int = 64

# Name prefixes (Cinder convention, mirrored on the backend).
VOLUME_PREFIX: str = 'volume-'
SNAPSHOT_PREFIX: str = 'snapshot-'

# Characters TrueNAS permits in an iSCSI target name.
_TARGET_ALLOWED = re.compile(r'[^a-z0-9.:-]')


# --- Unit conversion ------------------------------------------------------


def gib_to_bytes(size_gib: int) -> int:
    """Convert a Cinder GiB size to bytes, clamped to the 1 GiB minimum."""
    return max(int(size_gib) * GIGABYTE, MIN_VOLUME_BYTES)


def bytes_to_gib(size_bytes: int) -> float:
    """Convert a byte count to GiB (may be fractional)."""
    return size_bytes / GIGABYTE


# --- Backend naming -------------------------------------------------------


def volume_name(name_id: str) -> str:
    """Backend volume name for a Cinder ``volume.name_id``.

    ``name_id`` (not ``id``) is used so the name survives migration/retype.
    """
    return f'{VOLUME_PREFIX}{name_id}'


def snapshot_name(name_id: str) -> str:
    """Backend snapshot short-name for a Cinder ``snapshot.name_id``."""
    return f'{SNAPSHOT_PREFIX}{name_id}'


def dataset_name(pool: str, dataset_root: str, name_id: str) -> str:
    """Full ZFS dataset path: ``<pool>/<dataset_root>/volume-<name_id>``.

    ``dataset_root`` may be empty (volumes created directly under the pool)
    or a nested path such as ``tank/cinder``.
    """
    parts = [pool.strip('/')]
    root = dataset_root.strip('/')
    if root:
        parts.append(root)
    parts.append(volume_name(name_id))
    return '/'.join(parts)


def snapshot_id(dataset: str, snap_name: str) -> str:
    """ZFS snapshot identifier: ``<dataset>@<snap_name>``."""
    return f'{dataset}@{snap_name}'


def zvol_device_path(dataset: str) -> str:
    """iSCSI extent ``disk`` value for a zvol: ``zvol/<dataset>``."""
    return f'zvol/{dataset}'


def _sanitize(raw: str, maxlen: int) -> str:
    """Lower-case, strip disallowed chars, and truncate to ``maxlen``."""
    cleaned = _TARGET_ALLOWED.sub('-', raw.lower())
    return cleaned[:maxlen]


def target_name(name_id: str) -> str:
    """Sanitized iSCSI target name (``[a-z0-9.:-]``, <=120 chars)."""
    return _sanitize(volume_name(name_id), TARGET_NAME_MAXLEN)


def extent_name(name_id: str) -> str:
    """Sanitized iSCSI extent name (<=64 chars)."""
    return _sanitize(volume_name(name_id), EXTENT_NAME_MAXLEN)


def target_iqn(iqn_base: str, target: str) -> str:
    """Fully-qualified target IQN: ``<iqn_base>:<target_name>``."""
    return f'{iqn_base}:{target}'
