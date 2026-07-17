# Truenas Cinder Driver

An OpenStack Cinder iSCSI volume driver for TrueNAS SCALE.

`TrueNASISCSIDriver` is a control-plane-only Cinder driver: it provisions
zvols, iSCSI extents, and targets on a TrueNAS SCALE appliance over the
TrueNAS WebSocket JSON-RPC API and returns iSCSI connection information to
Cinder. All host-side attach/detach/multipath is handled by `os-brick`; the
driver never mounts anything on the host.

Feature set: Cinder's mandatory iSCSI feature set **plus multipath and
multiattach**.

## Quick start

For the full option reference and `cinder.conf` details see
[`docs/truenas-driver.rst`](docs/truenas-driver.rst). The short version:

**1. On the TrueNAS SCALE appliance** (API `v25.10.0` or later):

- Create an API key for a user that can manage datasets and iSCSI sharing
  (*Credentials → Local Users → API Keys*).
- Enable the iSCSI service, set its base name (IQN), and note the ID of the
  portal you want to publish targets on.
- Have a ZFS pool ready for Cinder volumes.

**2. On each `cinder-volume` host**, install the TrueNAS API client. It is not
on a package index yet, so install from a pinned git tag — replace the
placeholder URL and tag with the values your storage team provides:

```console
pip install "truenas-api-client @ git+https://git.example.com/truenas/api_client.git@<PINNED_TAG>"
```

**3. Add a backend to `cinder.conf`** and enable it:

```ini
[DEFAULT]
enabled_backends = truenas

[truenas]
volume_driver = cinder.volume.drivers.truenas.driver.TrueNASISCSIDriver
volume_backend_name = truenas
san_ip = 192.0.2.10
san_login = admin
truenas_api_key = 1-AbCdEf...
truenas_pool = tank
truenas_dataset_root = cinder
truenas_iscsi_portal_id = 1
target_prefix = iqn.2005-10.org.freenas.ctl
```

Restart `cinder-volume` to apply the change.

**4. Create a volume type and a volume** to verify:

```console
openstack volume type create truenas \
  --property volume_backend_name=truenas
openstack volume create --type truenas --size 1 test-volume
```

The driver creates a zvol under `<truenas_pool>/<truenas_dataset_root>/` and
exports it over iSCSI when the volume is attached.

## Licensing

The driver code is **Apache-2.0** (see `LICENSE`) so it can be merged in-tree
into `openstack/cinder`. It imports the TrueNAS API client lazily: the client
is **not** a hard dependency and is **not** in cinder's global-requirements,
so operators install it out-of-band (see the Quick start above).

## Development

This repo is `uv`-managed:

```console
uv sync                # create the venv and install (dev) dependencies
uv run pytest          # unit tests
uv run ruff check      # lint
uv run ruff format     # format
uv run basedpyright    # type-check (strict)
```
