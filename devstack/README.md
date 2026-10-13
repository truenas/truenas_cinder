# Testing with DevStack

Run a single-node OpenStack with the TrueNAS Cinder driver as its only volume
backend, then exercise it by hand and with Tempest.

| File | What it is |
| ---- | ---------- |
| `prepare-vm.sh` | Prepares a fresh Ubuntu 24.04 or 26.04 VM and writes `local.conf` |
| `local.conf.example` | The DevStack configuration `prepare-vm.sh` fills in |
| `plugin.sh`, `settings` | A DevStack plugin: copies the driver into DevStack's cinder tree and installs the TrueNAS API client before `cinder-volume` starts |

## What you need

- **A dedicated Ubuntu 24.04 or 26.04 VM.** DevStack makes substantial changes to the
  machine it runs on, so don't use a workstation or a shared host. Give it
  16 GiB of RAM (8 GiB is the floor), 4 or more vCPUs, about 80 GiB of disk,
  and nested virtualization so instances can run under KVM. Without nested
  virtualization the script falls back to QEMU emulation, which works but is
  slow.
- **Network access from the VM to TrueNAS** on TCP 443 (API) and 3260
  (iSCSI).
- **The TrueNAS settings** from the main
  [Quick start](../README.md#what-youll-need): address, API key, pool, and
  portal ID.
- **A checkout of this repository on the VM**, cloned or copied however you
  like. DevStack uses its last commit, so commit any changes you want tested.

## Set it up

From the checkout on the VM, export your settings. These are the same names
`tools/smoke_test.py` uses:

```console
export TRUENAS_IP=192.0.2.10
export TRUENAS_API_KEY=16-AbCdEf...
export TRUENAS_POOL=tank                  # default: tank
export TRUENAS_INSECURE_SKIP_VERIFY=true  # only for a self-signed certificate
```

Preview first. This checks the VM and TrueNAS and prints the `local.conf` it
would write, without changing anything:

```console
./devstack/prepare-vm.sh --dry-run
```

Then prepare the VM. `-E` keeps your exports:

```console
sudo -E ./devstack/prepare-vm.sh
```

This installs the iSCSI initiator and multipath tools, checks your TrueNAS
settings with the smoke test, creates DevStack's `stack` user, clones DevStack,
copies this repository to `/opt/stack/src/truenas_cinder`, and writes
`/opt/stack/devstack/local.conf`. It stops before touching DevStack if TrueNAS
rejects the settings. Run `./devstack/prepare-vm.sh --help` for every option.

Start DevStack. It takes 20 to 40 minutes:

```console
sudo -u stack -i
cd devstack && ./stack.sh
```

Or pass `--stack` to `prepare-vm.sh` to run it straight away.

During `stack.sh`, the plugin first checks that TrueNAS answers, so a wrong
address fails in the first minute rather than at the end. Once cinder is
installed, it copies the driver in with `tools/sync_to_cinder.py` and installs
the TrueNAS API client, before `cinder-volume` is configured or started.

## Check that it works

As the `stack` user:

```console
source ~/devstack/openrc admin admin
openstack volume service list            # cinder-volume@truenas: "up"
```

Then walk a volume through its lifecycle, checking TrueNAS after each step.
Volumes appear as zvols under `<pool>/devstack`:

```console
openstack volume create --size 1 vol1
openstack volume snapshot create --volume vol1 snap1
openstack volume create --snapshot snap1 vol2      # clone of the snapshot
openstack volume create --source vol1 vol3         # clone of the volume
openstack volume set --size 2 vol1                 # extend

# Attach to a real instance
IMAGE=$(openstack image list -f value -c Name | grep -m1 cirros)
openstack server create --flavor m1.tiny --image "$IMAGE" \
  --network private --wait vm1
openstack server add volume vm1 vol1
openstack volume show vol1 -c status -c attachments
```

## Run Tempest

As the `stack` user, start with the volume API tests, then the Cinder plugin's
tests:

```console
cd /opt/stack/tempest
tox -e all -- '^tempest\.api\.volume'
tox -e all -- '^cinder_tempest_plugin'
```

Read the skip list as well as the failures. A skipped test means a feature is
switched off in Tempest's configuration, not that the driver supports it.
`local.conf` turns on the revert and extend-while-attached tests explicitly,
because DevStack only enables them for its own LVM backend.

## Change the driver and test again

After committing a change to your checkout on the VM, the quick way is to
copy the driver straight into the running DevStack and restart
`cinder-volume`:

```console
python3 tools/sync_to_cinder.py /opt/stack/cinder --force
sudo systemctl restart devstack@c-vol
```

To redeploy from scratch instead, re-run `sudo -E ./devstack/prepare-vm.sh`,
then `./unstack.sh && ./stack.sh` as the `stack` user. `prepare-vm.sh` removes
DevStack's previous copy of the plugin so the new commit is used; DevStack
otherwise never updates a plugin it has already cloned.

## Plugin settings

`local.conf` sets these, and you can override them there. The plugin's
`settings` file supplies the defaults.

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `TRUENAS_IP` | (required) | TrueNAS address |
| `TRUENAS_API_KEY` | (required) | TrueNAS API key |
| `TRUENAS_LOGIN` | `truenas_admin` | User that owns the key |
| `TRUENAS_POOL` | `tank` | ZFS pool for volumes |
| `TRUENAS_DATASET_ROOT` | `devstack` | Dataset the volumes go under |
| `TRUENAS_PORTAL_ID` | `1` | iSCSI portal ID |
| `TRUENAS_VERIFY_SSL` | `True` | Verify the TrueNAS certificate |
| `TRUENAS_MULTIPATH` | `False` | Use multipath; set `True` once TrueNAS has a second portal address |
| `TRUENAS_SYNC_DRIVER` | `True` | Copy the driver from this repository. Set `False` when the cinder tree under test already contains it, such as the driver's own review |
| `TRUENAS_API_CLIENT_REF` | `TS-25.10.7` | Release tag of the TrueNAS API client to install |

## Troubleshooting

- **`cinder-volume@truenas` is down.** Its log names the reason:
  `sudo journalctl -u devstack@c-vol | grep -i truenas`. The main README's
  [troubleshooting table](../README.md#troubleshooting) maps each message to
  a fix.
- **`stack.sh` stops at "Checking TrueNAS settings".** `TRUENAS_IP` or
  `TRUENAS_API_KEY` is missing from `local.conf`, or TrueNAS doesn't answer
  on TCP 443 from the VM.
- **Attaching is slow or fails with multipath on.** With a single TrueNAS
  portal address there is only one path. Leave `TRUENAS_MULTIPATH=False`
  until TrueNAS has a second address.
- **Leftover zvols on TrueNAS.** `unstack.sh` and `clean.sh` don't delete
  volumes on TrueNAS. Delete them in OpenStack before unstacking, or remove
  `<pool>/devstack` on TrueNAS afterwards.
