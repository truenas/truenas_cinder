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

==============
TrueNAS driver
==============

The TrueNAS volume driver provisions each Cinder volume as a ZFS zvol on a
TrueNAS SCALE system and exports it over iSCSI. The driver is control-plane
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
- Retype a volume within the backend, including to a multiattach type.
- Thin provisioning.
- Multipath.
- Multiattach.

Prerequisites
~~~~~~~~~~~~~

- A TrueNAS SCALE system advertising API version ``v25.10.0`` or later,
  reachable from every ``cinder-volume`` host over ``wss`` (HTTPS).

- The ``websocket-client`` Python package, 1.3.2 or later, on each
  ``cinder-volume`` host. The driver includes its own TrueNAS API client;
  this is the one library it needs. Many environments already have it::

    pip install "websocket-client>=1.3.2"

  Install it into the **same Python environment that runs**
  ``cinder-volume``. Installing into the shell's default ``python`` has no
  effect when the service runs in a virtualenv or a container; for
  containerized deployments, add the install to the ``cinder-volume`` image.
  To target the service's own interpreter and verify the result::

    CINDER_PY=$(head -1 "$(command -v cinder-volume)" | sed 's/^#!//')
    sudo "$CINDER_PY" -m pip install "websocket-client>=1.3.2"
    "$CINDER_PY" -c "import websocket; print('websocket-client OK')"

  If the package is missing, the driver fails ``check_for_setup_error`` with
  an actionable message rather than an import traceback.

- A TrueNAS **API key** for a user with permission to manage datasets and
  iSCSI sharing (*Credentials* → *Local Users* → pick the user → *API Keys*).
  Set ``truenas_login`` to the user that owns it -- on current TrueNAS SCALE
  usually ``truenas_admin`` rather than ``root``. TrueNAS checks that the key
  belongs to that user and rejects a mismatch.

- An **iSCSI portal** configured on TrueNAS (*Shares* → *Block (iSCSI)* →
  *Portals*). Note its ID; the portal's listen addresses are read at
  connection time to build the multipath portal list. A wildcard
  (``0.0.0.0``) bind is replaced with ``truenas_ip``.

- The TrueNAS iSCSI service enabled, with a base name (IQN prefix) set in
  its global configuration. The driver reads the base name and the portal
  ports from TrueNAS, so neither is configured in ``cinder.conf``.

- TCP 443 (API) and 3260 (iSCSI) reachable from every ``cinder-volume`` host.

Configuration
~~~~~~~~~~~~~

Add a backend stanza to ``cinder.conf`` and reference it from
``enabled_backends``:

.. code-block:: ini

   [DEFAULT]
   enabled_backends = truenas

   [truenas]
   volume_driver = cinder.volume.drivers.truenas.driver.TrueNASDriver
   volume_backend_name = truenas
   # Connection
   truenas_ip = 192.0.2.10
   truenas_login = truenas_admin
   truenas_api_key = 16-AbCdEf...
   truenas_verify_ssl = true
   # Storage layout
   truenas_pool = tank
   truenas_dataset_root = cinder
   truenas_iscsi_portal_id = 1
   # Provisioning
   truenas_sparse = true
   truenas_volblocksize = 16K
   truenas_compression = LZ4
   # Optional CHAP
   use_chap_auth = false

``truenas_login`` should name the user that owns ``truenas_api_key``. When
``truenas_api_url`` is not set, the endpoint is derived as
``wss://<truenas_ip>/api/current``.

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
       from ``truenas_ip`` when unset.
   * - ``truenas_ip``
     - ``''``
     - Hostname or IP of the TrueNAS system.
   * - ``truenas_login``
     - ``truenas_admin``
     - TrueNAS user that owns the API key. TrueNAS checks that the key
       belongs to this user.
   * - ``truenas_api_key``
     - (none)
     - TrueNAS API key used to authenticate.
   * - ``truenas_auth_mechanism``
     - ``PLAIN``
     - API-key mechanism. ``PLAIN`` sends the key over the TLS-protected
       connection and works with TrueNAS SCALE 25.10 and later. ``SCRAM``
       proves knowledge of the key without sending it, makes TrueNAS prove
       the same, and binds the login to TrueNAS's TLS certificate, so a
       man in the middle cannot complete it; it needs TrueNAS 26 or later.
       Never negotiated automatically.
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

The driver also honours the standard ``use_chap_auth`` option.

Cinder's generic ``target_prefix`` option is **not** used. TrueNAS publishes
every target as ``<base name>:<target name>``, so the driver reads the base
name from TrueNAS's iSCSI global configuration, and each portal address's
port from the portal itself.

Troubleshooting
~~~~~~~~~~~~~~~

The backend refuses to start rather than half-working, and
``cinder-volume.log`` names the reason.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Message
     - Cause and fix
   * - ``websocket-client could not be imported``
     - The message says why. ``No module named 'websocket'`` means it is
       absent from the environment the service runs in: install it with
       ``cinder-volume``'s own interpreter. Any other missing module means
       the install is incomplete.
   * - ``TrueNAS rejected the API key``
     - The key is wrong or has been revoked, or ``truenas_login`` does not
       name the key's owner (try ``truenas_admin``).
   * - ``The API key has expired``
     - Create a new key in TrueNAS and update ``truenas_api_key``.
   * - ``TrueNAS does not offer SCRAM login``
     - TrueNAS predates 26. Leave ``truenas_auth_mechanism`` at ``PLAIN``.
   * - ``Channel binding is not defined for the signature algorithm``
     - SCRAM binds the login to TrueNAS's TLS certificate, which cannot be
       done for an Ed25519 certificate. Use an RSA or ECDSA certificate.
   * - ``TrueNAS pool "..." was not found``
     - ``truenas_pool`` is misspelled or the pool is not imported.
   * - ``TrueNAS iSCSI portal N was not found``
     - ``truenas_iscsi_portal_id`` does not match a configured portal.
   * - ``TrueNAS did not report an iSCSI base name``
     - Set a base name in TrueNAS's iSCSI global configuration. There is
       nothing to set in ``cinder.conf``.
   * - ``certificate verify failed``
     - TrueNAS presents a self-signed certificate; set
       ``truenas_verify_ssl = false`` on a trusted network.
   * - Volume creates but will not attach
     - Check ``open-iscsi`` and ``multipathd`` on the compute host and that
       TCP 3260 is reachable; the driver only provisions, ``os-brick``
       performs the login.
