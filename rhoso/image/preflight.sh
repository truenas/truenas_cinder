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
# The preflight step of the truenas-cinder component's certification, as
# Partner Connect lays it out: build the cinder-volume image with Docker,
# push it to Red Hat's inbound registry, and run preflight on it. Results go
# to Red Hat only with --submit.
#
# The tag is VERSION-RELEASE from context.sh (set RELEASE for a rebuild of
# the same driver version). Needs RH_CINDER_REGISTRY_KEY, the component's
# registry key, and with --submit PYXIS_API_KEY. Preflight's results and log
# land in ~/.cache/truenas-cinder-preflight/<tag>.
#
# Usage: preflight.sh [--submit]
set -euo pipefail

component=6ad6a4a45b504d03518346e2
repository=quay.io/redhat-isv-containers/$component
robot=redhat-isv-containers+$component-robot

submit=()
case "${1:-}" in
    --submit)
        : "${PYXIS_API_KEY:?set PYXIS_API_KEY}"
        submit=(--submit "--certification-component-id=$component")
        ;;
    '') ;;
    *) echo "usage: $0 [--submit]" >&2; exit 2 ;;
esac
: "${RH_CINDER_REGISTRY_KEY:?set RH_CINDER_REGISTRY_KEY}"

here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/context" "$work/docker"

"$here/context.sh" "$work/context"
build_args=()
while IFS= read -r arg; do
    build_args+=(--build-arg "$arg")
done < "$work/context/build-args"
version=$(sed -n 's/^VERSION=//p' "$work/context/build-args")
release=$(sed -n 's/^RELEASE=//p' "$work/context/build-args")
image=$repository:$version-$release

docker build --pull "${build_args[@]}" -t "$image" "$work/context"

# The robot login goes in a throwaway Docker config, so it neither needs nor
# replaces the user's own quay.io login; preflight reads it from there.
printf '%s' "$RH_CINDER_REGISTRY_KEY" \
    | docker --config "$work/docker" login quay.io -u "$robot" \
        --password-stdin
docker --config "$work/docker" push "$image"

artifacts=$HOME/.cache/truenas-cinder-preflight/$version-$release
mkdir -p "$artifacts"
PFLT_PYXIS_API_TOKEN=${PYXIS_API_KEY:-} \
PFLT_DOCKERCONFIG=$work/docker/config.json \
PFLT_ARTIFACTS=$artifacts \
PFLT_LOGFILE=$artifacts/preflight.log \
    preflight check container "$image" "${submit[@]}"
