#!/bin/bash
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
# DevStack plugin for the TrueNAS Cinder driver.
#
#   stack pre-install  Stop early if the TrueNAS settings are missing or the
#                      TrueNAS API is unreachable, rather than after a full run.
#   stack install      Copy the driver into DevStack's cinder tree and install
#                      the TrueNAS API client. This runs after cinder is
#                      installed but before cinder-volume is configured or
#                      started, so the backend comes up on its first start.

# shellcheck disable=SC2154  # CINDER_DIR and the TRUENAS_* settings come from DevStack

function truenas_check_settings {
    local var
    for var in TRUENAS_IP TRUENAS_API_KEY; do
        if [[ -z "${!var:-}" ]]; then
            die $LINENO "$var must be set in local.conf to use the TrueNAS backend"
        fi
    done
    if ! curl -fsk --max-time 15 "https://${TRUENAS_IP}/api/versions" >/dev/null; then
        die $LINENO "Cannot reach the TrueNAS API at https://${TRUENAS_IP}/api/versions"
    fi
}

function truenas_sync_driver {
    # cinder is installed in editable mode, so files copied into its source
    # tree take effect. --force because this tree belongs to DevStack: always
    # deploy the checkout DevStack cloned.
    python3 "$TRUENAS_CINDER_DIR/tools/sync_to_cinder.py" "$CINDER_DIR" --force >/dev/null
}

function truenas_install_api_client {
    # No spaces around '@': pip_install word-splits its arguments, so the
    # usual "name @ url" form would reach pip as three arguments.
    pip_install "truenas-api-client@git+${TRUENAS_API_CLIENT_REPO}@${TRUENAS_API_CLIENT_REF}"
}

if is_service_enabled c-vol; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        echo_summary "Checking TrueNAS settings"
        truenas_check_settings
    elif [[ "$1" == "stack" && "$2" == "install" ]]; then
        if [[ "$TRUENAS_SYNC_DRIVER" == "True" ]]; then
            echo_summary "Copying the TrueNAS driver into $CINDER_DIR"
            truenas_sync_driver
        fi
        echo_summary "Installing the TrueNAS API client"
        truenas_install_api_client
    fi
fi
