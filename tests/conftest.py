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

Shows which TrueNAS API client release a run used, so the output of
tools/test_client_versions.sh says what each pass actually tested. stestr
ignores this file and tools/sync_to_cinder.py does not copy it.
"""

import json
from importlib import metadata
from typing import Any, cast

from truenas_cinder import client as tn_client


def _client_source() -> str:
    """The installed client's git revision, e.g. ``TS-25.10.7 (8c59978)``."""
    try:
        dist = metadata.distribution('truenas-api-client')
    except metadata.PackageNotFoundError:
        return 'not installed'
    raw = dist.read_text('direct_url.json')
    if not raw:
        return f'version {dist.version}'
    info = cast('dict[str, Any]', json.loads(raw))
    vcs = cast('dict[str, Any]', info.get('vcs_info') or {})
    revision = str(vcs.get('requested_revision') or 'main branch')
    commit = str(vcs.get('commit_id') or '')[:7]
    return f'{revision} ({commit})' if commit else revision


def pytest_report_header() -> list[str]:
    client = tn_client.TrueNASClient
    if not client.dependency_available():
        login = f'cannot be imported: {client.dependency_error()}'
    elif client.supports_auth_mechanisms():
        login = 'selectable PLAIN/SCRAM login'
    else:
        login = 'key-only login (TS-25.10 generation)'
    return [f'truenas_api_client: {_client_source()} -- {login}']
