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
"""pytest-only configuration.

Shows the Python version and whether websocket-client is installed, so the
output of tools/run_tests.sh says what each pass actually tested. stestr
ignores this file and tools/sync_to_cinder.py does not copy it.
"""

from __future__ import annotations

import platform
from importlib import metadata


def pytest_report_header() -> list[str]:
    try:
        websocket = f'websocket-client {metadata.version("websocket-client")}'
    except metadata.PackageNotFoundError:
        websocket = 'websocket-client not installed'
    return [f'Python {platform.python_version()}, {websocket}']
