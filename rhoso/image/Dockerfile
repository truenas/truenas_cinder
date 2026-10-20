# Lab-only cinder-volume image: RHOSO 18's image plus the TrueNAS driver.
# Red Hat allows a partner image to add only dependencies; a certified driver
# must ship in RHOSO itself, so this image is for testing, not certification.
# build.sh fills truenas/ with the driver modules rewritten to their in-tree
# package, cinder.volume.drivers.truenas. The base image already has the one
# dependency, websocket-client (python3-websocket-client 1.3.3 in 18.0).
FROM registry.redhat.io/rhoso/openstack-cinder-volume-rhel9:18.0

LABEL name="cinder-volume-truenas" \
      vendor="TrueNAS" \
      summary="RHOSO 18 cinder-volume with the TrueNAS driver (lab build)"

USER root

COPY truenas/ /tmp/truenas/
RUN set -e; \
    drivers=$(python3 -c 'import os, cinder.volume.drivers as d; print(os.path.dirname(d.__file__))'); \
    rm -rf "$drivers/truenas"; \
    cp -r /tmp/truenas "$drivers/truenas"; \
    rm -rf /tmp/truenas; \
    python3 -m compileall -q "$drivers/truenas"; \
    python3 -c 'from cinder.volume.drivers.truenas import client, driver; assert client.DEPENDENCY_AVAILABLE'

USER cinder
