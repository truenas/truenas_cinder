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

> **Set `truenas_login` to the user that owns the key.** TrueNAS checks that
> the key belongs to that user and rejects a mismatch. On current TrueNAS
> SCALE the owner is usually `truenas_admin`, not `root` or `admin`.

### 1. Install websocket-client

The driver talks to TrueNAS with its own client, which needs one library,
`websocket-client`. Many OpenStack environments already have it; if not:

```console
pip install "websocket-client>=1.3.2"
```

**Install it into the same Python environment `cinder-volume` runs in.** This
is the most common mistake: installing into your shell's default `python`
does nothing if the service runs in a venv or a container. To target the
service's own interpreter:

```console
# Resolve the interpreter cinder-volume actually uses, then install with it
CINDER_PY=$(head -1 "$(command -v cinder-volume)" | sed 's/^#!//')
sudo "$CINDER_PY" -m pip install "websocket-client>=1.3.2"

# Confirm that interpreter can import it
"$CINDER_PY" -c "import websocket; print('websocket-client OK')"
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
# Set false only for a self-signed certificate
truenas_verify_ssl = true
# On TrueNAS 26 or later, SCRAM logs in without sending the key
#truenas_auth_mechanism = SCRAM
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
| `websocket-client could not be imported` | The message says why. `No module named 'websocket'` means step 1 went into the wrong environment: re-run it with the service's own interpreter. Any other missing module means the install is incomplete: reinstall it. |
| `TrueNAS rejected the API key` | The key is wrong or was revoked, or `truenas_login` is not the key's owner (try `truenas_admin`). |
| `The API key has expired` | Create a new key in TrueNAS and update `truenas_api_key`. |
| `TrueNAS does not offer SCRAM login` | TrueNAS predates 26. Leave `truenas_auth_mechanism` at its `PLAIN` default. |
| `Channel binding is not defined for the signature algorithm` | SCRAM binds the login to TrueNAS's TLS certificate, which cannot be done for an Ed25519 certificate. Use an RSA or ECDSA certificate in TrueNAS. |
| `TrueNAS pool "…" was not found` | `truenas_pool` is misspelled, or the pool is not imported. |
| `TrueNAS iSCSI portal N was not found` | Wrong `truenas_iscsi_portal_id` — check *Shares → Block (iSCSI) → Portals*. |
| `TrueNAS did not report an iSCSI base name` | Set a base name in TrueNAS's iSCSI global configuration. The driver reads it from TrueNAS; there is nothing to set in `cinder.conf`. |
| certificate verify failed | Self-signed certificate; set `truenas_verify_ssl = false`. |

## Licensing

The driver code, including its TrueNAS client, is **Apache-2.0** (see
`LICENSE`) so it can be merged in-tree into `openstack/cinder`. Its one
third-party library, `websocket-client`, is already an optional dependency
of cinder for another driver.

## Development

This repo is `uv`-managed:

```console
uv sync                # create the venv and install (dev) dependencies
uv run pytest          # unit tests
uv run ruff check      # lint
uv run ruff format     # format
uv run basedpyright    # type-check (strict)
```

The driver also has to load without `websocket-client`, which is how
cinder's own test run may see it, and to run on Python 3.9, the Python of
Red Hat OpenStack Services on OpenShift 18. `uv run pytest` covers only the
dev environment, so before committing, run every variant:

```console
./tools/run_tests.sh              # extra arguments go to pytest
```

To run the driver in a real OpenStack, see [`devstack/README.md`](devstack/README.md):
it prepares a dedicated VM and runs DevStack with TrueNAS as the only volume
backend.
