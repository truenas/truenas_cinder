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
# Sourced by apply.sh and cert-prereqs.sh: load_settings ENV reads an
# environment's settings file and exports each setting.
#
# ENV is the name of rhoso/env/ENV.env, or a path to a settings file. The
# file holds NAME=value lines; blank lines and lines starting with # are
# ignored, and quotes around a value are dropped. Nothing is expanded or run,
# so a value is taken exactly as written (kickstart.py reads the same format).
#
# Sets SETTINGS_FILE to the file read and SETTINGS_NAMES to its setting
# names.

load_settings() {
    local env=${1:?an environment name or settings file is required}
    local dir line name value
    dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    SETTINGS_FILE=$env
    if [ ! -f "$SETTINGS_FILE" ]; then
        SETTINGS_FILE=$dir/env/$env.env
    fi
    if [ ! -f "$SETTINGS_FILE" ]; then
        echo "error: no settings file $env or $SETTINGS_FILE" >&2
        return 1
    fi
    SETTINGS_NAMES=()
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        if [[ $line =~ ^[[:space:]]*(#|$) ]]; then
            continue
        fi
        if [[ ! $line =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
            echo "error: $SETTINGS_FILE: not NAME=value: $line" >&2
            return 1
        fi
        name=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
            value=${BASH_REMATCH[1]}
        fi
        export "$name=$value"
        SETTINGS_NAMES+=("$name")
    done < "$SETTINGS_FILE"
}
