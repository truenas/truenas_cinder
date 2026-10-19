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
# Create the data plane's secrets in the openstack namespace, without printing
# any of their values:
#   dataplane-ansible-ssh-private-key-secret  Ansible's key for cloud-admin
#                                             (EDPM_SSH_KEY, default
#                                             ~/.ssh/rhoso-edpm)
#   nova-migration-ssh-key                    a new key for live migration
#   redhat-registry                           the registry.redhat.io login
#                                             from the cluster pull secret
#   libvirt-secret                            a random libvirt password
# Existing secrets are left alone.
set -euo pipefail

namespace=openstack
key=${EDPM_SSH_KEY:-$HOME/.ssh/rhoso-edpm}

exists() { oc get secret "$1" -n "$namespace" >/dev/null 2>&1; }

if ! exists dataplane-ansible-ssh-private-key-secret; then
    oc create secret generic dataplane-ansible-ssh-private-key-secret \
        -n "$namespace" \
        --from-file=ssh-privatekey="$key" \
        --from-file=ssh-publickey="$key.pub" \
        --from-file=authorized_keys="$key.pub"
fi

if ! exists nova-migration-ssh-key; then
    dir=$(mktemp -d)
    ssh-keygen -q -t ecdsa -N '' -C nova-migration -f "$dir/id"
    oc create secret generic nova-migration-ssh-key -n "$namespace" \
        --from-file=ssh-privatekey="$dir/id" \
        --from-file=ssh-publickey="$dir/id.pub"
    rm -rf "$dir"
fi

if ! exists redhat-registry; then
    oc get secret pull-secret -n openshift-config \
        -o go-template='{{index .data ".dockerconfigjson"}}' \
        | base64 -d \
        | uv run --no-project python -c '
import base64, json, sys
auth = json.load(sys.stdin)["auths"]["registry.redhat.io"]["auth"]
user, _, password = base64.b64decode(auth).decode().partition(":")
logins = {"registry.redhat.io": {user: password}}
sys.stdout.write(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "redhat-registry", "namespace": sys.argv[1]},
    "type": "Opaque",
    "stringData": {"edpm_container_registry_logins": json.dumps(logins)},
}))
' "$namespace" \
        | oc create -f -
fi

if ! exists libvirt-secret; then
    oc create secret generic libvirt-secret -n "$namespace" \
        --from-literal=LibvirtPassword="$(openssl rand -hex 16)"
fi
