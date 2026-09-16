# TrueNAS Cinder Driver

An OpenStack Cinder driver for TrueNAS SCALE.

`TrueNASDriver` is a control-plane-only Cinder driver: it provisions
zvols, iSCSI extents, and targets on TrueNAS SCALE over the
TrueNAS WebSocket JSON-RPC API and returns iSCSI connection information to
Cinder. All host-side attach/detach/multipath is handled by `os-brick`; the
driver never mounts anything on the host.

Feature set: Cinder's mandatory feature set **plus multipath and
multiattach**.

## Quick start

Full option reference: [`docs/truenas-driver.rst`](docs/truenas-driver.rst).

### What you'll need

Collect these five values from TrueNAS first — each one goes straight into
`cinder.conf`:

| Value | Where to find it | Example |
| ----- | ---------------- | ------- |
| Address | — | `192.0.2.10` |
| API key | *Credentials → Local Users →* pick the user *→ API Keys → Add* | `16-AbCdEf…` |
| **Username owning that key** | the same user you just added the key to | `truenas_admin` |
| ZFS pool | *Storage* | `tank` |
| iSCSI portal ID | *Shares → Block (iSCSI) → Portals*, leftmost column | `1` |

Also confirm TrueNAS runs API `v25.10.0` or later, the iSCSI service is
enabled with a base name (IQN) set, and that TCP 443 and 3260 are reachable
from every `cinder-volume` host.

> **The username matters.** API-key authentication fails unless
> `truenas_login` is the exact user the key belongs to. On current TrueNAS
> SCALE that is usually `truenas_admin` — **`root` and `admin` will fail**
> with `Invalid API key`.

### 1. Install the TrueNAS API client

The driver imports the client lazily and it is not in cinder's
global-requirements, so you install it yourself. It is not on PyPI yet, so
install from a release tag — `TS-25.10.7` matches a 25.10 system:

```console
pip install "truenas-api-client @ git+https://github.com/truenas/api_client.git@TS-25.10.7"
```

**Install it into the same Python environment `cinder-volume` runs in.** This
is the most common mistake: installing into your shell's default `python`
does nothing if the service runs in a venv or a container. To target the
service's own interpreter:

```console
# Resolve the interpreter cinder-volume actually uses, then install with it
CINDER_PY=$(head -1 "$(command -v cinder-volume)" | sed 's/^#!//')
sudo "$CINDER_PY" -m pip install \
  "truenas-api-client @ git+https://github.com/truenas/api_client.git@TS-25.10.7"

# Confirm that interpreter can import it
"$CINDER_PY" -c "import truenas_api_client; print('client OK')"
```

For containerized deployments, add the same `pip install` to the
`cinder-volume` image instead of installing at runtime.

### 2. Check connectivity before touching Cinder

From a checkout of this repo, this talks to TrueNAS directly and validates the
address, API key, username, pool, and portal in one shot — much faster than
debugging through `cinder-volume` logs:

```console
export TRUENAS_URL=wss://192.0.2.10/api/current
export TRUENAS_API_KEY=16-AbCdEf...
export TRUENAS_USER=truenas_admin
export TRUENAS_POOL=tank

uv run python tools/smoke_test.py
```

It is read-only and changes nothing. See [`tools/README.md`](tools/README.md)
for the optional `--lifecycle` mode.

### 3. Configure the backend

Add the stanza to `cinder.conf` and enable it:

```ini
[DEFAULT]
enabled_backends = truenas

[truenas]
volume_driver = cinder.volume.drivers.truenas.driver.TrueNASDriver
volume_backend_name = truenas
truenas_ip = 192.0.2.10
truenas_login = truenas_admin
truenas_api_key = 16-AbCdEf...
truenas_pool = tank
truenas_dataset_root = cinder
truenas_iscsi_portal_id = 1
target_prefix = iqn.2005-10.org.freenas.ctl
# Set false only for a self-signed certificate
truenas_verify_ssl = true
```

Restart `cinder-volume`, then confirm the backend came up:

```console
openstack volume service list   # cinder-volume should be "up"
```

### 4. Create a volume

```console
openstack volume type create truenas \
  --property volume_backend_name=truenas
openstack volume create --type truenas --size 1 test-volume
```

The driver creates a zvol at `<truenas_pool>/<truenas_dataset_root>/volume-…`
and exports it over iSCSI when the volume is attached.

### Troubleshooting

If the backend does not start, `cinder-volume.log` names the exact reason:

| Message | Cause and fix |
| ------- | ------------- |
| `truenas_api_client is not installed` | Step 1 went into the wrong environment. Re-run it with the service's own interpreter. |
| `Invalid API key` | `truenas_login` is not the user the key belongs to (try `truenas_admin`), or the key was revoked. |
| `SCRAM authentication is not supported` | TrueNAS predates 26. Leave `truenas_auth_mechanism` at its `PLAIN` default. |
| `TrueNAS pool "…" was not found` | `truenas_pool` is misspelled, or the pool is not imported. |
| `TrueNAS iSCSI portal N was not found` | Wrong `truenas_iscsi_portal_id` — check *Shares → Block (iSCSI) → Portals*. |
| `target_prefix … must be configured` | Set the IQN base in `cinder.conf`, and a base name on TrueNAS. |
| certificate verify failed | Self-signed certificate; set `truenas_verify_ssl = false`. |

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
