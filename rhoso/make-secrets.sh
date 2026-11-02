#!/usr/bin/env bash
# Copyright (c) 2026 TrueNAS
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Create the control plane's secrets in the openstack namespace, without
# printing any of their values:
#   osp-secret                     random service and database passwords
#   cinder-volume-truenas-secrets  the TrueNAS login and API key, as a
#                                  cinder.conf snippet with a section for
#                                  each backend (truenas, truenas2)
# An existing osp-secret is left alone: its passwords are already in use.
# The TrueNAS secret is replaced on every run, since it comes entirely from
# the environment. Needs TRUENAS_API_KEY; TRUENAS_LOGIN defaults to
# truenas_admin.
set -euo pipefail

namespace=openstack

random() { openssl rand -hex 16; }

if ! oc get secret osp-secret -n "$namespace" >/dev/null 2>&1; then
    {
        cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: osp-secret
  namespace: $namespace
type: Opaque
stringData:
  BarbicanSimpleCryptoKEK: "$(openssl rand -base64 32)"
  HeatAuthEncryptionKey: "$(random)"
EOF
        for key in \
            AdminPassword AodhPassword AodhDatabasePassword \
            BarbicanDatabasePassword BarbicanPassword CeilometerPassword \
            CinderDatabasePassword CinderPassword DatabasePassword \
            DbRootPassword DesignateDatabasePassword DesignatePassword \
            GlanceDatabasePassword GlancePassword HeatDatabasePassword \
            HeatPassword IronicDatabasePassword \
            IronicInspectorDatabasePassword IronicInspectorPassword \
            IronicPassword KeystoneDatabasePassword ManilaDatabasePassword \
            ManilaPassword MetadataSecret NeutronDatabasePassword \
            NeutronPassword NovaAPIDatabasePassword \
            NovaAPIMessageBusPassword NovaCell0DatabasePassword \
            NovaCell0MessageBusPassword NovaCell1DatabasePassword \
            NovaCell1MessageBusPassword NovaPassword \
            OctaviaDatabasePassword OctaviaHeartbeatKey OctaviaPassword \
            PlacementDatabasePassword PlacementPassword SwiftPassword \
            WatcherDatabasePassword WatcherPassword; do
            echo "  $key: \"$(random)\""
        done
    } | oc create -f -
fi

: "${TRUENAS_API_KEY:?set TRUENAS_API_KEY}"
verb=create
if oc get secret cinder-volume-truenas-secrets -n "$namespace" \
        >/dev/null 2>&1; then
    verb=replace
fi
{
    cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: cinder-volume-truenas-secrets
  namespace: $namespace
type: Opaque
stringData:
  truenas-secrets.conf: |
EOF
    for backend in truenas truenas2; do
        cat <<EOF
    [$backend]
    truenas_login = ${TRUENAS_LOGIN:-truenas_admin}
    truenas_api_key = $TRUENAS_API_KEY
EOF
    done
} | oc "$verb" -f -
