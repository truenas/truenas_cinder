# `sync_to_cinder.py`

One-time bootstrap that copies the TrueNAS driver and its unit tests from this
dev repo into an [`openstack/cinder`](https://opendev.org/openstack/cinder)
checkout, rewriting the dev-repo imports to their in-tree package paths so the
result is ready for the initial Gerrit push.

## When to run it

Run it **once**, to seed a fresh cinder fork for the upstream merge /
third-party-CI track. After the first review round the fork/Gerrit becomes the
authoritative copy of the driver module (reviewers push edits there, you
address comments there). This tool is a **bootstrap, not an ongoing two-way
sync** — do not expect the dev repo to stay current with the fork after
submission. See `PLAN.md` (“Source-of-truth handoff”) for the full rationale.

## Prerequisites

- A local `openstack/cinder` checkout (your fork). The tool validates that the
  path contains `cinder/volume/drivers/`.
- Nothing else — the script is stdlib-only and needs no dependencies. Running
  it via `uv run` is convenient but optional:

  ```console
  uv run python tools/sync_to_cinder.py /path/to/cinder --dry-run
  ```

## Usage

```console
python tools/sync_to_cinder.py CINDER_ROOT [--dry-run] [--force]
```

| Argument / flag | Meaning |
| --------------- | ------- |
| `CINDER_ROOT`   | Path to the cinder checkout (fork) root. |
| `--dry-run`     | Print what would be copied without writing anything. |
| `--force`       | Overwrite destination files that already exist. |

Recommended flow:

```console
# 1. Preview
python tools/sync_to_cinder.py ~/src/cinder --dry-run

# 2. Seed the fork
python tools/sync_to_cinder.py ~/src/cinder
```

By default, destination files that **already exist are skipped** (reported as
`Skipped (exists; use --force)`). This protects edits already made in the fork.
Pass `--force` only when you deliberately want to overwrite them — it clobbers
any in-fork changes.

## What it copies

| Dev repo (source of truth)             | In-tree destination |
| -------------------------------------- | ------------------- |
| `src/truenas_cinder/__init__.py`       | `cinder/volume/drivers/truenas/__init__.py` |
| `src/truenas_cinder/common.py`         | `cinder/volume/drivers/truenas/common.py` |
| `src/truenas_cinder/exception.py`      | `cinder/volume/drivers/truenas/exception.py` |
| `src/truenas_cinder/opts.py`           | `cinder/volume/drivers/truenas/opts.py` |
| `src/truenas_cinder/client.py`         | `cinder/volume/drivers/truenas/client.py` |
| `src/truenas_cinder/driver.py`         | `cinder/volume/drivers/truenas/driver.py` |
| `tests/unit/fakes.py`                  | `cinder/tests/unit/volume/drivers/truenas/fakes.py` |
| `tests/unit/test_common.py`            | `cinder/tests/unit/volume/drivers/truenas/test_common.py` |
| `tests/unit/test_driver.py`            | `cinder/tests/unit/volume/drivers/truenas/test_driver.py` |
| _(generated)_                          | `cinder/tests/unit/volume/drivers/truenas/__init__.py` |

## Import rewrites

Every copied file has its dev-repo package references rewritten to the in-tree
package path:

| Dev repo token   | In-tree replacement |
| ---------------- | ------------------- |
| `truenas_cinder` | `cinder.volume.drivers.truenas` |
| `tests.unit`     | `cinder.tests.unit.volume.drivers.truenas` |

The rewrite is word-boundary matched, so it does **not** touch the lazily
imported `truenas_api_client` dependency or the `truenas_*` config option
names (e.g. `truenas_pool`, `truenas_api_url`).

## What it does NOT do

The tool copies code only. The rest of the in-tree submission is manual — it
prints this checklist after a successful run:

1. Add a reno note: `releasenotes/notes/truenas-iscsi-driver-*.yaml`
2. Declare the backend in `doc/source/reference/support-matrix.ini` and add
   `doc/source/configuration/block-storage/drivers/truenas-driver.rst`
3. Regenerate options: `tox -e genopts`
4. Verify interface compliance: `tox -e compliance`
5. Style — the final authority (ruff only approximates OpenStack `hacking`):
   `tox -e pep8`. Fix any import-order (H3xx) nits the mechanical rewrite left
   behind.
6. Run the ported unit tests: `tox -e py3 -- volume.drivers.truenas`

> **Note on style:** the dev repo's ruff config approximates OpenStack
> `hacking`/pep8 but cannot reproduce it exactly. `tox -e pep8` in the fork is
> the final authority — run it early rather than at submission time.

## Not synced

The dev repo's `stubs/` (hand-written cinder type stubs for basedpyright) and
`pyproject.toml` are dev-only and are intentionally left behind — the cinder
tree provides the real packages and its own tooling.
