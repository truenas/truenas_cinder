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
# Create what Red Hat's cinder certification suite assumes exists but RHOSO
# doesn't create, through the openstackclient pod:
#   - the swiftoperator role: the suite gives it to every test user, and
#     without Swift it doesn't exist, so every test would fail at setup;
#   - the multiattach volume type the suite's multi-attach tests use;
#   - the external network "public" for the floating IPs the suite logs in
#     to its test servers through (EXTERNAL_* settings). It also needs a
#     gateway chassis: edpm_enable_chassis_gw in 13-data-plane.yaml.
# Anything that already exists is left alone.
#
# Usage: cert-prereqs.sh ENV
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=settings.sh
. "$here/settings.sh"

[ $# -eq 1 ] || { echo "usage: $0 ENV" >&2; exit 2; }
load_settings "$1"
for name in EXTERNAL_PHYSNET EXTERNAL_CIDR EXTERNAL_GATEWAY \
        EXTERNAL_POOL_START EXTERNAL_POOL_END DNS_VIP; do
    if [ -z "${!name:-}" ]; then
        echo "error: $name is not set in $SETTINGS_FILE" >&2
        exit 1
    fi
done

oc rsh -n openstack openstackclient env \
    EXTERNAL_PHYSNET="$EXTERNAL_PHYSNET" \
    EXTERNAL_CIDR="$EXTERNAL_CIDR" \
    EXTERNAL_GATEWAY="$EXTERNAL_GATEWAY" \
    EXTERNAL_POOL_START="$EXTERNAL_POOL_START" \
    EXTERNAL_POOL_END="$EXTERNAL_POOL_END" \
    DNS_VIP="$DNS_VIP" \
    bash -s <<'EOF'
set -euo pipefail
if ! openstack role show swiftoperator >/dev/null 2>&1; then
    openstack role create swiftoperator >/dev/null
    echo "created role swiftoperator"
fi
if ! openstack volume type show multiattach >/dev/null 2>&1; then
    openstack volume type create multiattach \
        --property multiattach='<is> True' >/dev/null
    echo "created volume type multiattach"
fi
if ! openstack network show public >/dev/null 2>&1; then
    openstack network create public --external --share \
        --provider-network-type flat \
        --provider-physical-network "$EXTERNAL_PHYSNET" >/dev/null
    echo "created network public"
fi
if ! openstack subnet show public-subnet >/dev/null 2>&1; then
    openstack subnet create public-subnet --network public --no-dhcp \
        --subnet-range "$EXTERNAL_CIDR" --gateway "$EXTERNAL_GATEWAY" \
        --allocation-pool "start=$EXTERNAL_POOL_START,end=$EXTERNAL_POOL_END" \
        --dns-nameserver "$DNS_VIP" >/dev/null
    echo "created subnet public-subnet"
fi
echo "role swiftoperator, volume type multiattach and network public exist"
EOF
