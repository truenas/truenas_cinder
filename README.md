# truenas_cinder

An OpenStack Cinder iSCSI volume driver for TrueNAS SCALE.

`TrueNASISCSIDriver` is a control-plane-only Cinder driver: it provisions
zvols, iSCSI extents, and targets on a TrueNAS SCALE appliance over the
TrueNAS WebSocket JSON-RPC API and returns iSCSI connection information to
Cinder. All host-side attach/detach/multipath is handled by `os-brick`; the
driver never mounts anything on the host.

Feature set: Cinder's mandatory iSCSI feature set **plus multipath and
multiattach**.

## Licensing (RBD model)

The driver code is **Apache-2.0** (see `LICENSE`) so it can be merged in-tree
into `openstack/cinder`. It lazily imports [`truenas_api_client`][tac]
(LGPLv3), exactly as `cinder/volume/drivers/rbd.py` lazily imports the `rbd`
bindings. `truenas_api_client` is **not** a hard dependency and is **not** in
global-requirements; operators install it out-of-band.

[tac]: https://github.com/truenas/api_client

## Development

This repo is `uv`-managed. See `CLAUDE.md` for the tooling rules and `PLAN.md`
for the full design and certification track.

```console
uv run pytest          # unit tests
uv run ruff check      # lint
uv run ruff format     # format
uv run basedpyright    # type-check (strict)
```
