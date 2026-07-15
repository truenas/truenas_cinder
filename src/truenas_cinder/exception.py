# Copyright (c) 2026 TrueNAS, Inc.
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
"""Driver-local exceptions.

These wrap the raw ``truenas_api_client`` transport/RPC errors into a small,
typed surface the driver reasons about. User-facing failures are raised as
``cinder.exception.VolumeBackendAPIException`` at the driver boundary.
"""


class TrueNASClientError(Exception):
    """Base for all TrueNAS client-side errors."""


class TrueNASConnectionError(TrueNASClientError):
    """The WebSocket connection could not be established or was lost."""


class TrueNASApiError(TrueNASClientError):
    """A TrueNAS JSON-RPC call returned an error.

    ``errno`` mirrors the TrueNAS ``CallError.errno`` when available so
    callers can distinguish e.g. ENOENT (treated as NotFound / idempotent
    success) from real failures.
    """

    def __init__(
        self,
        message: str,
        errno: int | None = None,
    ) -> None:
        super().__init__(message)
        self.errno: int | None = errno


class TrueNASNotFound(TrueNASApiError):
    """The referenced TrueNAS object does not exist.

    Raised (or mapped from an ENOENT ``TrueNASApiError``) so delete paths can
    treat "already gone" as success.
    """
