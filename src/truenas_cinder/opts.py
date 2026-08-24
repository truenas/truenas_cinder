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
"""oslo.config option definitions for the TrueNAS Cinder driver.

The driver owns its own option group rather than inheriting the SAN option
group (whose SSH/local-exec options are dead weight for a WebSocket-API
backend). ``target_prefix`` (the IQN base) and ``use_chap_auth`` are *not*
redefined here -- they are supplied by Cinder's base volume options and are
folded in via ``TrueNASISCSIDriver.get_driver_options``.
"""

from oslo_config import cfg

truenas_opts: list[cfg.Opt] = [
    # --- Connection ------------------------------------------------------
    cfg.StrOpt(
        'truenas_api_url',
        default=None,
        help='Full WebSocket URL to the TrueNAS API endpoint, e.g. '
        '"wss://truenas.example.com/api/current". If unset it is '
        'derived from san_ip and truenas_verify_ssl.',
    ),
    cfg.StrOpt(
        'san_ip',
        default='',
        help='Hostname or IP address of the TrueNAS SCALE appliance. Used '
        'to build the API URL when truenas_api_url is not set.',
    ),
    cfg.StrOpt(
        'san_login',
        default='truenas_admin',
        help='TrueNAS username that owns the configured API key. This is '
        'required for API-key authentication; it is not necessarily "root" '
        '(recent TrueNAS SCALE releases use "truenas_admin").',
    ),
    cfg.StrOpt(
        'truenas_api_key',
        default=None,
        secret=True,
        deprecated_name='san_password',
        help='TrueNAS API key used with login_with_api_key. Create it under '
        'Credentials -> Local Users -> API Keys.',
    ),
    cfg.BoolOpt(
        'truenas_verify_ssl',
        default=True,
        help='Verify the TrueNAS server TLS certificate. Set False only for '
        'self-signed certificates in trusted networks.',
    ),
    cfg.StrOpt(
        'truenas_auth_mechanism',
        default='PLAIN',
        choices=('PLAIN', 'SCRAM'),
        help='API-key authentication mechanism. PLAIN transmits the API key '
        'over the TLS-protected connection and is what TrueNAS SCALE 25.10 '
        'supports. SCRAM never transmits the key but requires a server that '
        'implements it (TrueNAS 26 and later). The mechanism is never '
        'negotiated automatically, so set it to match the appliance.',
    ),
    # --- Storage layout --------------------------------------------------
    cfg.StrOpt(
        'truenas_pool',
        default=None,
        help='Name of the ZFS pool on TrueNAS that backs Cinder volumes.',
    ),
    cfg.StrOpt(
        'truenas_dataset_root',
        default='cinder',
        help='Dataset path under truenas_pool where volume zvols are '
        'created (e.g. "cinder" -> <pool>/cinder/volume-<id>). May be '
        'nested; ancestors are created as needed.',
    ),
    cfg.IntOpt(
        'truenas_iscsi_portal_id',
        default=1,
        min=1,
        help='ID of the TrueNAS iSCSI portal to publish targets on. The '
        "portal's listen IPs are read at connection time to build the "
        'multipath portal list.',
    ),
    # --- Provisioning ----------------------------------------------------
    cfg.BoolOpt(
        'truenas_sparse',
        default=True,
        help='Create sparse (thin-provisioned) zvols. When False, zvols are '
        'thick (refreservation set to the full volume size).',
    ),
    cfg.StrOpt(
        'truenas_volblocksize',
        default='16K',
        help='ZFS volblocksize for new zvols (e.g. 512, 4K, 16K, 64K, 128K).',
    ),
    cfg.StrOpt(
        'truenas_compression',
        default='LZ4',
        help='ZFS compression algorithm for new zvols (e.g. LZ4, ZSTD, OFF).',
    ),
]
