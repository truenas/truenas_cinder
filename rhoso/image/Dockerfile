# The TrueNAS cinder-volume image for RHOSO 18: Red Hat's cinder-volume image
# plus the driver. The driver is out-of-tree for RHOSO 18, so this image is
# what Red Hat certifies (preflight) alongside it; rebuild it for every RHOSO
# 18 z-stream. The base image already has the driver's one dependency,
# websocket-client (python3-websocket-client 1.3.3 in 18.0).
#
# context.sh fills truenas/ with the driver modules rewritten to their
# in-tree package, cinder.volume.drivers.truenas, so cinder.conf names the
# driver as it will once the driver is upstream.
FROM registry.redhat.io/rhoso/openstack-cinder-volume-rhel9:18.0

# The driver's version (TrueNASDriver.VERSION), this image's build number for
# that version, and the source commit. context.sh writes them to build-args.
ARG VERSION=dev
ARG RELEASE=1
ARG REVISION=unknown

# Replaces the identity labels inherited from Red Hat's image.
LABEL name="truenas-cinder" \
      vendor="TrueNAS" \
      version="${VERSION}" \
      release="${RELEASE}" \
      summary="TrueNAS Cinder driver for Red Hat OpenStack Services on OpenShift 18" \
      description="Red Hat OpenStack Services on OpenShift 18 cinder-volume with the TrueNAS block storage driver, for TrueNAS iSCSI storage." \
      io.k8s.display-name="TrueNAS OpenStack Cinder Driver" \
      io.k8s.description="Cinder volume service with the TrueNAS driver for Red Hat OpenStack Services on OpenShift 18" \
      io.openshift.tags="rhoso,openstack,cinder,truenas,iscsi" \
      com.redhat.component="truenas-cinder-container" \
      maintainer="TrueNAS <support@truenas.com>" \
      url="https://www.truenas.com" \
      vcs-ref="${REVISION}" \
      org.opencontainers.image.revision="${REVISION}" \
      konflux.additional-tags=""

USER root

COPY LICENSE /licenses/LICENSE
COPY truenas/ /tmp/truenas/
# python3 -B: the import checks must not write bytecode into the cinder tree
# that Red Hat's packages own, which preflight would report as modified.
RUN set -e; \
    drivers=$(python3 -B -c 'import os, cinder.volume.drivers as d; print(os.path.dirname(d.__file__))'); \
    rm -rf "$drivers/truenas"; \
    cp -r /tmp/truenas "$drivers/truenas"; \
    rm -rf /tmp/truenas; \
    python3 -m compileall -q "$drivers/truenas"; \
    python3 -B -c 'from cinder.volume.drivers.truenas import client, driver; assert client.DEPENDENCY_AVAILABLE'

USER cinder
