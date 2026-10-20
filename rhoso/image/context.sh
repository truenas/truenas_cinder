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
# Fill DIR with the image's build context: the Dockerfile, LICENSE, and
# the driver modules in truenas/ with their imports rewritten to
# cinder.volume.drivers.truenas, as the sync tool does. Also writes
# DIR/build-args, one NAME=value per line:
#   VERSION   TrueNASDriver.VERSION
#   RELEASE   $RELEASE, default 1: the image's build number for that version
#   REVISION  the git commit, with -dirty when the tree has local changes
#
# Usage: context.sh DIR
set -euo pipefail

dir=${1:?usage: context.sh DIR}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)

cp "$here/Dockerfile" "$repo/LICENSE" "$dir/"
mkdir -p "$dir/truenas"
(cd "$repo" && uv run python - "$dir/truenas" <<'EOF'
import sys
from pathlib import Path

sys.path.insert(0, 'tools')
from sync_to_cinder import DRIVER_MODULES, SRC_PKG, rewrite_imports

dest = Path(sys.argv[1])
for name in DRIVER_MODULES:
    text = (SRC_PKG / name).read_text(encoding='utf-8')
    (dest / name).write_text(rewrite_imports(text), encoding='utf-8')
EOF
)

version=$(sed -n "s/^    VERSION = '\(.*\)'$/\1/p" \
    "$repo/src/truenas_cinder/driver.py")
revision=$(git -C "$repo" rev-parse HEAD)
if [ -n "$(git -C "$repo" status --porcelain -- src rhoso/image)" ]; then
    revision="$revision-dirty"
fi
cat > "$dir/build-args" <<EOF
VERSION=${version:?TrueNASDriver.VERSION not found}
RELEASE=${RELEASE:-1}
REVISION=$revision
EOF
