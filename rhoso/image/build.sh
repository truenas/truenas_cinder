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
# Build the cinder-volume image (Dockerfile) in the cluster that oc points
# at, into its internal registry, and roll it out to the truenas backend.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
namespace=openstack

# The build pulls its base image from registry.redhat.io with a copy of the
# cluster's global pull secret.
if ! oc get secret redhat-registry-pull -n "$namespace" >/dev/null 2>&1; then
    auth=$(oc get secret pull-secret -n openshift-config \
        -o go-template='{{index .data ".dockerconfigjson"}}')
    oc create -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: redhat-registry-pull
  namespace: $namespace
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: $auth
EOF
    unset auth
fi

oc apply -f "$here/../10-cinder-volume-image.yaml"

context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
"$here/context.sh" "$context"
build_args=()
while IFS= read -r arg; do
    build_args+=(--build-arg "$arg")
done < "$context/build-args"

oc start-build cinder-volume-truenas -n "$namespace" \
    --from-dir="$context" "${build_args[@]}" --follow --wait

# Pods pull IfNotPresent, so a rebuild under the same tag would never reach
# them: point the backend at the new image by digest, which rolls it out.
image=$(oc get istag cinder-volume-truenas:18.0 -n "$namespace" \
    -o jsonpath='{.image.dockerImageReference}')
echo "Built $image"
if oc get openstackversion openstack -n "$namespace" >/dev/null 2>&1; then
    oc patch openstackversion openstack -n "$namespace" --type merge -p \
        "{\"spec\":{\"customContainerImages\":{\"cinderVolumeImages\":{\"truenas\":\"$image\"}}}}"
fi
