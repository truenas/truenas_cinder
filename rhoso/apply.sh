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
# Fill in the ${NAME} placeholders in RHOSO manifests from an environment's
# settings file (see settings.sh and env/example.env), then apply them with
# oc. Only the settings the file declares are filled in; any placeholder left
# over is an error, so a missing setting never reaches the cluster.
#
# Usage:
#   apply.sh ENV FILE... [-- OC_APPLY_ARGUMENTS...]
#   apply.sh --render ENV FILE...       print the manifests instead
#
# A FILE that isn't found as given is looked for next to this script, so
# "apply.sh crc-lab 12-control-plane.yaml" works from anywhere. Example:
#   rhoso/apply.sh crc-lab 12-control-plane.yaml -- --dry-run=server
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=settings.sh
. "$here/settings.sh"

usage() {
    echo "usage: $0 [--render] ENV FILE... [-- OC_APPLY_ARGUMENTS...]" >&2
    exit 2
}

render=false
if [ "${1:-}" = --render ]; then
    render=true
    shift
fi
[ $# -ge 2 ] || usage
env=$1
shift
files=()
oc_args=()
while [ $# -gt 0 ]; do
    if [ "$1" = -- ]; then
        shift
        oc_args=("$@")
        break
    fi
    if [ -f "$1" ]; then
        files+=("$1")
    elif [ -f "$here/$1" ]; then
        files+=("$here/$1")
    else
        echo "error: no such file: $1" >&2
        exit 1
    fi
    shift
done
[ ${#files[@]} -gt 0 ] || usage

if ! command -v envsubst >/dev/null; then
    echo "error: envsubst not found (it's in the gettext package)" >&2
    exit 1
fi

load_settings "$env"
declared=$(printf '${%s} ' "${SETTINGS_NAMES[@]}")
rendered=$(
    for file in "${files[@]}"; do
        printf -- '---\n'
        envsubst "$declared" < "$file"
    done
)
missing=$(printf '%s\n' "$rendered" \
    | grep -o '\${[A-Za-z_][A-Za-z0-9_]*}' | sort -u | tr '\n' ' ' || true)
if [ -n "$missing" ]; then
    echo "error: not set in $SETTINGS_FILE: $missing" >&2
    exit 1
fi

if $render; then
    printf '%s\n' "$rendered"
else
    printf '%s\n' "$rendered" | oc apply -f - "${oc_args[@]}"
fi
