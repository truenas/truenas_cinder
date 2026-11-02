# RHOSO 18 test environment

Manifests and scripts that deploy Red Hat OpenStack Services on OpenShift
(RHOSO) 18 with TrueNAS as Cinder's storage, trimmed to what Red Hat's
Cinder certification suite needs. They were built on a single-node OpenShift
Local (CRC) lab with one compute VM; the official certification run needs a
multi-node cluster, which uses the same files with its own settings (and its
own versions of the two node-specific files, `06` and `13`).

The image Cinder runs the driver from, and its preflight checks:
[`image/README.md`](image/README.md).

| File | What it is |
| ---- | ---------- |
| `00`–`02` | The operators RHOSO needs (NMState, MetalLB, cert-manager) and the OpenStack operator |
| `03-openstack-init.yaml` | The OpenStack operator's init resource; scales unused service operators to zero |
| `04-iscsid-machineconfig.yaml` | Enables iscsid on the OpenShift nodes (reboots them) |
| `05-namespace.yaml` | The `openstack` namespace |
| `06-nncp.yaml` | The OpenShift node's RHOSO networks (written for one node) |
| `07`–`09` | Pod attachments, MetalLB VIP pools, and RHOSO's IP allocation for each network |
| `10-cinder-volume-image.yaml` | In-cluster build of the cinder-volume image, for development (`image/build.sh`) |
| `11-openstack-version.yaml` | Runs both TrueNAS backends from the certified image |
| `12-control-plane.yaml` | The control plane: Keystone, Glance, Cinder (two TrueNAS backends, NFS backup), Nova, Neutron, Placement, Barbican |
| `13-data-plane.yaml` | The compute node (written for one node) |
| `14-tempest-cinder.yaml` | A quick Cinder API Tempest run, outside the certification suite |
| `apply.sh`, `settings.sh` | Fill in a file's settings and apply it |
| `env/example.env` | Every setting, documented |
| `make-secrets.sh` | The control plane's passwords and the TrueNAS API key secret |
| `make-dataplane-secrets.sh` | The data plane's SSH keys, registry login, and libvirt password |
| `cert-prereqs.sh` | What the certification suite assumes exists |
| `kickstart.py` | Unattended RHEL install media for a compute VM |

## Settings

Addresses and other per-environment values live in a settings file,
`env/<name>.env`, not in the manifests. Start from the example:

```console
cp rhoso/env/example.env rhoso/env/mylab.env    # then edit it
```

Git ignores every `.env` file in `env/` except `example.env`, so a real
environment's addresses stay out of the repository. Secrets don't go in the
file at all: `make-secrets.sh` reads `TRUENAS_API_KEY` from the shell.

The manifests hold `${NAME}` placeholders, and `apply.sh` fills them in and
applies the result. Only the settings the file declares are filled in, and a
placeholder left over stops it before anything reaches the cluster:

```console
rhoso/apply.sh mylab 12-control-plane.yaml                       # apply
rhoso/apply.sh mylab 12-control-plane.yaml -- --dry-run=server   # oc apply arguments after --
rhoso/apply.sh --render mylab 12-control-plane.yaml              # print it instead
```

## Deploying

Roughly in file order, with `oc` logged in as a cluster administrator:

1. **Operators:** `oc apply -f` `00`, then `01` once NMState and MetalLB are
   installed, then `02`. The OpenStack operator's install plan is manual:
   approve it (`oc get installplan -n openstack-operators`, then
   `oc patch installplan <name> -n openstack-operators --type merge -p
   '{"spec":{"approved":true}}'`), then apply `03`.
2. **Nodes:** `04` (each node reboots) and `05`.
3. **Networks:** `apply.sh ENV` with `06`, `07`, `08`, and `09`.
4. **TrueNAS:** create the dataset `TRUENAS_BACKUP_PATH` and an NFS share
   for it that allows the address TrueNAS sees the OpenShift nodes as. Cinder
   creates the backends' datasets itself.
5. **Image:** import the certified image as shown at the top of `11`, with
   a pull secret for Red Hat's certification registry.
6. **Control plane:** `make-secrets.sh`, then `apply.sh ENV` with `11` and
   `12`, and wait: `oc wait openstackcontrolplane openstack -n openstack
   --for=condition=Ready --timeout=30m`.
7. **Data plane:** install the compute node (for a VM,
   `uv run --with pycdlib python rhoso/kickstart.py ENV ks.iso` makes
   unattended install media), run `make-dataplane-secrets.sh`, then
   `apply.sh ENV 13-data-plane.yaml` and wait for the deployment. Then map
   the compute into Nova:
   `oc rsh -n openstack nova-cell0-conductor-0 nova-manage cell_v2 discover_hosts --verbose`.
8. **Certification prerequisites:** `cert-prereqs.sh ENV`.

### On OpenShift Local

- The VM must start without its second NIC (the one in `OCP_NODE_NIC`):
  OpenShift Local's own networking doesn't come up with it attached. Add it
  after `crc start` and remove it after `crc stop`; never reboot the VM with
  it attached, so a change that reboots the node (`04`) means stop, start,
  and re-add.
- After the NIC is re-added, delete the nmstate handler pod
  (`oc delete pod -n openshift-nmstate -l component=kubernetes-nmstate-handler`)
  so it re-applies `06`, which failed while the NIC was missing.
- `06` pins the bridge's MAC to the NIC's (`OCP_NODE_NIC_MAC`). Without it
  MetalLB announces the VIPs with the bridge's earlier random MAC, and
  nothing outside the node reaches them: the compute's DNS, and with it its
  package installs, fails first.
- The cluster's API listens only on the VM's host. A test host on the lab
  network needs it forwarded there.

## Certification run

Red Hat's suite (`rhoso-cert-cinder`) runs four steps: **volumes**
(mandatory), **backups**, **multi-attach**, and **consistency groups** (the
last two optional; the driver doesn't support consistency groups, so that
step fails, which Red Hat accepts for an optional step).

### What the cluster needs first

Red Hat's suite assumes all of these; RHOSO creates none of them:

1. **The `swiftoperator` role.** The suite gives it to every test user; it
   doesn't exist without Swift, and then every test fails at setup.
   (`cert-prereqs.sh`)
2. **A `multiattach` volume type**, with `multiattach="<is> True"`.
   (`cert-prereqs.sh`)
3. **An external network with floating IPs**, and a gateway chassis for it.
   The suite logs in to its test servers over SSH. (`cert-prereqs.sh` makes
   the network; `13` makes the compute the gateway.)
4. **Cinder backup.** The mandatory volumes step includes backup tests.
   (`12`: NFS on TrueNAS)
5. **Two backends**, and Tempest told about them, for the volume migration
   tests (see the overrides below). (`12`: `truenas` and `truenas2`)
6. **With a single compute node only:** Nova allowed to resize onto the same
   host (`NOVA_ALLOW_RESIZE_TO_SAME_HOST`). Tempest still marks resize
   unavailable with one compute, so the multi-attach resize test is skipped.

### The test host

Any RHEL 9 system with `oc` logged in to the cluster; the lab used the
compute node. As root:

```console
subscription-manager repos --enable=cert-1-for-rhel-9-x86_64-rpms
dnf install redhat-certification redhat-certification-rhoso
mkdir rhoso-cert && cd rhoso-cert
```

In that directory, a `tempest-conf-overrides` file, one setting per line, no
comments:

```text
volume-feature-enabled.multi_backend True
volume.backend_names truenas,truenas2
volume.vendor_name TrueNAS
volume.storage_protocol iSCSI
```

### Running it

```console
rhoso-cert-init       # namespace: openstack; storage class: STORAGE_CLASS; suite: 1 (Cinder)
rhoso-cert-run        # about 25 minutes
rhoso-cert-logs       # include the must-gather report when submitting
rhoso-cert-save
rhcert-cli login
rhcert-cli upload --certification-id <ID> --file /var/log/redhat-certification/<file>.tgz
rhoso-cert-cleanup    # before another run
```

To rerun only some tests, copy
`/usr/share/redhat-certification-rhoso/rhoso-cert-debug.yaml` into the
directory, replace the tests under `includeList` with the ones to rerun, and
run `rhoso-cert-init` with suite 7 (Debug), then `rhoso-cert-run`.

**On OpenShift Local only**, the suite's Tempest pod doesn't fit: it asks
for 4 CPUs by default. For a rehearsal, copy
`/usr/share/redhat-certification-rhoso/rhoso-cert-cinder.yaml` into the
directory and add, under `spec:`,

```yaml
  resources:
    requests: {cpu: "1", memory: 1Gi}
    limits: {cpu: "4", memory: 4Gi}
```

`rhoso-cert-run` uses a copy in the current directory over the packaged one.
The official run uses the packaged file unchanged.
