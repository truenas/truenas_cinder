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
# Run the unit tests the ways the driver has to pass them:
#
#   default       the dev environment
#   no-websocket  without websocket-client, which is how cinder's own test
#                 run may see the driver: the import is guarded, so the
#                 driver must still load and its tests must still pass
#
# Arguments are passed to pytest:
#
#   ./tools/run_tests.sh
#   ./tools/run_tests.sh -q -k Session

# No errexit: run every variant, then report them all.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

failed=()

run_variant() {
    local name=$1
    shift
    printf '\n=== %s ===\n' "$name"
    if "$@"; then
        printf -- '--- %s: passed\n' "$name"
    else
        printf -- '--- %s: FAILED\n' "$name"
        failed+=("$name")
    fi
}

without_websocket() {
    # Its own environment: the dev one, minus websocket-client.
    export UV_PROJECT_ENVIRONMENT=.venv-no-websocket
    uv sync -q &&
        uv pip uninstall -q --python .venv-no-websocket/bin/python \
            websocket-client &&
        uv run --no-sync pytest "$@"
}

run_variant default uv run pytest "$@"
run_variant no-websocket without_websocket "$@"

printf '\n'
if ((${#failed[@]})); then
    printf 'FAILED with: %s\n' "${failed[*]}"
    exit 1
fi
printf 'Passed in every variant.\n'
