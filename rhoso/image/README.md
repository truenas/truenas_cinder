# The RHOSO cinder-volume image

The TrueNAS driver is not part of Red Hat OpenStack Services on OpenShift
(RHOSO) 18, so it ships in its own image: Red Hat's cinder-volume image plus
the driver. Red Hat's
[RHOSO certification policy](https://docs.redhat.com/en/documentation/red_hat_software_certification/2025/html-single/red_hat_openstack_services_on_openshift_certification_policy_guide/index)
calls this an out-of-tree driver: the image has to pass Red Hat's container
certification, which is what preflight checks, before the driver can be
certified with Red Hat's test plan.

| File | What it is |
| ---- | ---------- |
| `Dockerfile` | The image: Red Hat's cinder-volume image, the driver, the license, and TrueNAS labels |
| `context.sh` | Fills a build context with the `Dockerfile`, `LICENSE`, the driver, and the build arguments |
| `preflight.sh` | Builds the image with Docker, pushes it to Red Hat's inbound registry, and runs preflight |
| `build.sh` | Builds the same image inside the lab's OpenShift cluster and rolls it out there (see `../12-control-plane.yaml`) |

## What's in the image

- **Base:** `registry.redhat.io/rhoso/openstack-cinder-volume-rhel9:18.0`. It
  already has the driver's one library, `websocket-client` (1.3.3 in 18.0),
  so the image adds no packages.
- **The driver** as the package `cinder.volume.drivers.truenas`, its in-tree
  name, so `volume_driver` in `cinder.conf` stays the same once the driver
  ships in RHOSO itself.
- **`/licenses/LICENSE`:** the driver's Apache-2.0 license.
- **Labels:** TrueNAS's name, vendor, and maintainer in place of the ones
  inherited from Red Hat's image; `version` is the driver's version,
  `release` the build number, and `vcs-ref` the commit it was built from.
- **User:** `cinder`, not root. RHOSO still runs cinder-volume privileged,
  with host device access, to attach volumes over iSCSI.

## Tags

An image's tag is `VERSION-RELEASE`, for example `0.1.0-1`:

- `VERSION` is `TrueNASDriver.VERSION` in `src/truenas_cinder/driver.py`.
  For a new version, change it there (with a line in the class's version
  history) and commit; the scripts read it from there, so the image's tag,
  its `version` label, and the version Cinder reports always match. The
  driver follows [Semantic Versioning](https://semver.org/) and stays on 0.x
  while its configuration options and the way it lays volumes out on TrueNAS
  may still change; 1.0.0 commits to keeping them stable.
- `RELEASE` is the image's build number for that version: 1 by default, set
  with the `RELEASE` environment variable. Raise it to rebuild the same driver
  version, for example on a new RHOSO 18 z-stream, and go back to 1 when
  `VERSION` changes.

Commit before submitting. The image records the commit it was built from,
and marks it `-dirty` when `src/` or this directory has uncommitted changes.

## Preflight

### One-time setup

**In Red Hat Partner Connect**, the product and its component:

| Setting | Value |
| ------- | ----- |
| Product type | OpenStack Infrastructure |
| Product | TrueNAS OpenStack Cinder Driver |
| Component type | OpenStack Infrastructure, service Cinder (Block Storage) |
| Component name | `truenas-cinder` (ID `6ad6a4a45b504d03518346e2`) |
| Containers already part of RHOSO? | No: the image is ours |
| Image type | Component image: RHOSO deploys it, users don't run it directly |
| Host level access | Privileged |

**On the machine that runs preflight:**

- **Docker**, logged in to `registry.redhat.io` with a Red Hat account so it
  can pull the base image (`docker login registry.redhat.io`).
- **[preflight](https://github.com/redhat-openshift-ecosystem/openshift-preflight)**
  on your `PATH`.
- **Two keys in the environment:**

  | Variable | What it is | Where to find it |
  | -------- | ---------- | ---------------- |
  | `RH_CINDER_REGISTRY_KEY` | The component's registry key | The component's preflight page in Partner Connect, "View Registry Key" |
  | `PYXIS_API_KEY` | Your Partner Connect API key; only `--submit` needs it | Partner Connect, account settings |

### Run it

```console
./rhoso/image/preflight.sh                       # check only
./rhoso/image/preflight.sh --submit              # check and submit the results to Red Hat
RELEASE=2 ./rhoso/image/preflight.sh --submit    # a rebuild of the same driver version
```

It follows the steps on the component's preflight page in Partner Connect:

1. Builds the image with `docker build --pull`, so it starts from the newest
   RHOSO 18 base image.
2. Logs in to `quay.io` as the component's robot account and pushes the image
   as `quay.io/redhat-isv-containers/6ad6a4a45b504d03518346e2:VERSION-RELEASE`.
   The login goes in a throwaway Docker configuration, so it doesn't replace
   your own `quay.io` login.
3. Runs `preflight check container` on the pushed image, and with `--submit`
   sends the results to the `truenas-cinder` component.

Preflight's `results.json` and `preflight.log` land in
`~/.cache/truenas-cinder-preflight/VERSION-RELEASE/`. After a submission, the
log has links to the results, the image, and its vulnerability scan in
Partner Connect.

### What passes

A check-only run reports these 10 checks, all passing. With `--submit`,
preflight skips `RunAsNonRoot`, because the component is marked Privileged,
and reports 9.

| Check | Why it passes |
| ----- | ------------- |
| `HasLicense` | `/licenses/LICENSE` |
| `HasUniqueTag` | The `VERSION-RELEASE` tag (never `latest`) |
| `LayerCountAcceptable` | Fewer than 40 layers |
| `HasNoProhibitedPackages` | Only Red Hat's packages, no kernel |
| `HasRequiredLabel` | name, vendor, version, release, summary, description, and maintainer are all set |
| `HasNoProhibitedLabels` | The name, vendor, and maintainer labels are TrueNAS's |
| `RunAsNonRoot` | The image runs as `cinder` |
| `HasModifiedFiles` | The driver is new files; nothing installed by Red Hat's packages changes. The build's import checks run with `python3 -B` so they write no bytecode into cinder's own files. |
| `BasedOnUbi` | Red Hat's cinder-volume image is built on RHEL |
| `HasProhibitedContainerName` | `truenas-cinder` |

After a submission, Red Hat also scans the image for known vulnerabilities;
the results are in Partner Connect. They come from the base image, and a
rebuild on a newer RHOSO 18 z-stream picks up the fixes.

### Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `open ~/.docker/contexts/meta: not a directory` | `~/.docker/contexts` is a file instead of a directory: move it aside and `mkdir ~/.docker/contexts` |
| `unauthorized` pulling `registry.redhat.io/...` | `docker login registry.redhat.io` |
| `unauthorized` or `denied` pushing to `quay.io` | `RH_CINDER_REGISTRY_KEY` is wrong or was regenerated in Partner Connect |
| `502 Bad Gateway` from `quay.io/v2/auth` | A temporary quay.io error: run it again |
| `HasRequiredLabel` fails | A label in the `Dockerfile` is empty, or the build arguments didn't reach it |
| `HasModifiedFiles` fails | Something in the build wrote into files Red Hat's packages installed |

## Recertification

The image has to be rebuilt and certified again for every RHOSO 18 z-stream
(policy section 4.3), so it carries that z-stream's fixes:

```console
RELEASE=2 ./rhoso/image/preflight.sh --submit
```
