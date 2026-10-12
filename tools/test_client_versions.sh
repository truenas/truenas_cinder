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
# Run the unit tests against both supported TrueNAS API client releases, and
# with no client at all:
#
#   client-main   the client's main branch (the default dev environment)
#   client-25-10  the TS-25.10 release the docs and DevStack install
#   no-client     no client installed, which is how cinder's own test run
#                 sees it (the client is not in global-requirements)
#
# The two releases authenticate differently and only one can be installed at
# a time, so each runs in its own environment; see the client-* dependency
# groups in pyproject.toml. Arguments are passed to pytest:
#
#   ./tools/test_client_versions.sh
#   ./tools/test_client_versions.sh -q -k Legacy

# No errexit: run every variant, then report them all.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

failed=()

run_variant() {
    local group=$1
    shift
    printf '\n=== %s ===\n' "$group"
    if "$@"; then
        printf -- '--- %s: passed\n' "$group"
    else
        printf -- '--- %s: FAILED\n' "$group"
        failed+=("$group")
    fi
}

run_variant client-main \
    uv run pytest "$@"
run_variant client-25-10 \
    env UV_PROJECT_ENVIRONMENT=.venv-client-25-10 \
    uv run --no-group client-main --group client-25-10 pytest "$@"
run_variant no-client \
    env UV_PROJECT_ENVIRONMENT=.venv-no-client \
    uv run --no-group client-main pytest "$@"

printf '\n'
if ((${#failed[@]})); then
    printf 'FAILED with: %s\n' "${failed[*]}"
    exit 1
fi
printf 'Passed with both TrueNAS API client releases and with none.\n'
