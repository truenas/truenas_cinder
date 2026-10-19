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
"""Driver-local exceptions.

The TrueNAS connection and API errors, as a small typed surface the driver
reasons about. User-facing failures are raised as
``cinder.exception.VolumeBackendAPIException`` at the driver boundary.
"""

from __future__ import annotations


class TrueNASClientError(Exception):
    """Base for all TrueNAS client-side errors."""


class TrueNASConnectionError(TrueNASClientError):
    """The WebSocket connection could not be established or was lost.

    ``request_sent`` says whether the failed call had already reached the
    connection. When it had not, TrueNAS cannot have run it, so it is safe
    to send again on a new connection.
    """

    def __init__(self, message: str, *, request_sent: bool = False) -> None:
        super().__init__(message)
        self.request_sent: bool = request_sent


class TrueNASAuthError(TrueNASClientError):
    """TrueNAS did not accept the login, or the login could not be done."""


class TrueNASTimeout(TrueNASClientError):
    """TrueNAS did not answer a call in time.

    TrueNAS may still have run the call, so it must not be sent again.
    """


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
