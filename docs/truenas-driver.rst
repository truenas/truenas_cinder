..
      Copyright (c) 2026 TrueNAS
      All Rights Reserved.

      Licensed under the Apache License, Version 2.0 (the "License"); you may
      not use this file except in compliance with the License. You may obtain
      a copy of the License at

          http://www.apache.org/licenses/LICENSE-2.0

      Unless required by applicable law or agreed to in writing, software
      distributed under the License is distributed on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied. See the License for the specific language governing
      permissions and limitations under the License.

==========================
TrueNAS SCALE iSCSI driver
==========================

The TrueNAS volume driver provisions each Cinder volume as a ZFS zvol on a
TrueNAS SCALE appliance and exports it over iSCSI. The driver is control-plane
only: it drives the TrueNAS WebSocket JSON-RPC API to create zvols, iSCSI
extents, and targets, and returns iSCSI connection information to Cinder. All
host-side attach, detach, and multipath handling is performed by ``os-brick``.

Supported operations
~~~~~~~~~~~~~~~~~~~~~~

- Create, delete, attach, and detach volumes.
- Create and delete volume snapshots.
- Create a volume from a snapshot.
- Create a cloned volume.
- Copy an image to a volume.
- Copy a volume to an image.
- Extend a volume.
- Revert a volume to its most recent snapshot.
- Thin provisioning.
- Multipath.
- Multiattach.

Prerequisites
~~~~~~~~~~~~~

- A TrueNAS SCALE appliance advertising API version ``v25.10.0`` or later,
  reachable from every ``cinder-volume`` host over ``wss`` (HTTPS).

- The ``truenas_api_client`` Python package installed on each
  ``cinder-volume`` host. It is **not** part of ``global-requirements`` and
  must be installed out of band::

    pip install truenas-api-client

  If it is missing, the driver fails ``check_for_setup_error`` with an
  actionable message.

- A TrueNAS **API key** for a user with permission to manage datasets and
  iSCSI sharing (create it under *Credentials* → *Local Users* → *API Keys*).

- An **iSCSI portal** configured on TrueNAS. Note its ID; the portal's listen
  addresses are read at connection time to build the multipath portal list.

- The TrueNAS iSCSI (SCST) service enabled, with a base IQN name configured.

Configuration
~~~~~~~~~~~~~

Add a backend stanza to ``cinder.conf`` and reference it from
``enabled_backends``:

.. code-block:: ini

   [DEFAULT]
   enabled_backends = truenas

   [truenas]
   volume_driver = cinder.volume.drivers.truenas.driver.TrueNASISCSIDriver
   volume_backend_name = truenas
   # Connection
   san_ip = 192.0.2.10
   san_login = admin
   truenas_api_key = 1-AbCdEf...
   truenas_verify_ssl = true
   # Storage layout
   truenas_pool = tank
   truenas_dataset_root = cinder
   truenas_iscsi_portal_id = 1
   # iSCSI / IQN base (do not use the deprecated iscsi_target_prefix alias)
   target_prefix = iqn.2005-10.org.freenas.ctl
   # Provisioning
   truenas_sparse = true
   truenas_volblocksize = 16K
   truenas_compression = LZ4
   # Optional CHAP
   use_chap_auth = false

``san_login`` must be the user that owns ``truenas_api_key``. When
``truenas_api_url`` is not set, the endpoint is derived as
``wss://<san_ip>/api/current``.

After changing ``cinder.conf``, restart the ``cinder-volume`` service.

CHAP authentication
~~~~~~~~~~~~~~~~~~~

Set ``use_chap_auth = true`` to require CHAP. The driver generates a per-volume
credential (a 12–16 character secret, as TrueNAS requires) and stores it in the
volume's ``provider_auth``; it is returned to ``os-brick`` at attach time. The
TrueNAS ``iscsi.auth`` record is created and torn down with the volume's iSCSI
export.

Multipath
~~~~~~~~~

When the connector requests multipath, or the portal has more than one listen
address, the driver returns parallel ``target_portals`` / ``target_iqns`` /
``target_luns`` lists (one entry per portal IP, same IQN, LUN 0) alongside the
singular keys, so ``multipathd`` can build a multipath device.

Driver options
~~~~~~~~~~~~~~

.. config-table::
   :config-target: TrueNAS

   cinder.volume.drivers.truenas.driver

The driver-specific options are:

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Option
     - Default
     - Description
   * - ``truenas_api_url``
     - (derived)
     - Full WebSocket API endpoint, e.g. ``wss://host/api/current``. Derived
       from ``san_ip`` when unset.
   * - ``san_ip``
     - ``''``
     - Hostname or IP of the TrueNAS appliance.
   * - ``san_login``
     - ``root``
     - TrueNAS user that owns the API key.
   * - ``truenas_api_key``
     - (none)
     - TrueNAS API key used to authenticate.
   * - ``truenas_verify_ssl``
     - ``true``
     - Verify the TrueNAS TLS certificate.
   * - ``truenas_pool``
     - (none)
     - ZFS pool that backs Cinder volumes. Required.
   * - ``truenas_dataset_root``
     - ``cinder``
     - Dataset path under the pool where zvols are created. May be nested;
       ancestors are created as needed.
   * - ``truenas_iscsi_portal_id``
     - ``1``
     - ID of the TrueNAS iSCSI portal to publish targets on.
   * - ``truenas_sparse``
     - ``true``
     - Create sparse (thin) zvols. When false, zvols are thick
       (``refreservation`` set to the full size).
   * - ``truenas_volblocksize``
     - ``16K``
     - ZFS ``volblocksize`` for new zvols.
   * - ``truenas_compression``
     - ``LZ4``
     - ZFS compression algorithm for new zvols.

The driver also honours the standard ``target_prefix`` (the iSCSI IQN base)
and ``use_chap_auth`` options.
