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
"""Logging in to TrueNAS with an API key.

Two mechanisms, both through ``auth.login_ex`` (TrueNAS 25.04 and later):

``PLAIN``
    Sends the username and API key over the TLS-protected connection.
    Works on every supported TrueNAS release.
``SCRAM``
    Proves knowledge of the key without sending it, and makes TrueNAS prove
    it too (see ``scram``). Needs TrueNAS 26 or later.

The mechanism is never negotiated down. TrueNAS lists the mechanisms it
offers before the client has logged in, so a man in the middle could remove
SCRAM from that list; falling back to PLAIN would then hand it the key.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, cast

from truenas_cinder import jsonrpc, scram
from truenas_cinder.exception import TrueNASApiError, TrueNASAuthError

AUTH_PLAIN = 'PLAIN'
AUTH_SCRAM = 'SCRAM'
AUTH_MECHANISMS: tuple[str, ...] = (AUTH_PLAIN, AUTH_SCRAM)

# TrueNAS issues API keys as "<id>-<secret>".
_API_KEY = re.compile(r'^(\d+)-(.+)$', re.DOTALL)


class Session(Protocol):
    """What logging in needs from a connection."""

    def call(self, method: str, *params: object) -> object: ...

    def peer_certificate(self) -> bytes | None: ...


def login(
    session: Session,
    username: str,
    api_key: str,
    mechanism: str,
    *,
    channel_binding: bool | None = None,
) -> None:
    """Log the connection in, or raise ``TrueNASAuthError``.

    ``channel_binding`` applies to SCRAM: None binds the login to the TLS
    certificate whenever there is one, True requires that, False turns it
    off.
    """
    if not api_key:
        raise TrueNASAuthError('No API key is configured.')
    selected = mechanism.upper()
    if selected == AUTH_PLAIN:
        _login_plain(session, username, api_key)
    elif selected == AUTH_SCRAM:
        _login_scram(session, username, api_key, channel_binding)
    else:
        raise TrueNASAuthError(
            f'Unknown authentication mechanism {mechanism!r}; expected one '
            f'of {", ".join(AUTH_MECHANISMS)}.'
        )


def _rejection(response_type: object, reply: dict[str, Any], user: str) -> str:
    if response_type == 'AUTH_ERR':
        return (
            f'TrueNAS rejected the API key. Check that it is correct and '
            f'that it belongs to the user {user!r}.'
        )
    if response_type == 'EXPIRED':
        return 'The API key has expired.'
    if response_type == 'DENIED':
        return f'The user {user!r} is not allowed to use the API.'
    if response_type == 'REDIRECT':
        urls = reply.get('urls')
        where = (
            ', '.join(str(u) for u in cast('list[object]', urls))
            if isinstance(urls, list)
            else 'the active controller'
        )
        return (
            f'Log in through the active TrueNAS controller instead: {where}.'
        )
    return f'TrueNAS answered the login with {response_type!r}.'


def _reply_dict(reply: object) -> dict[str, Any]:
    if not isinstance(reply, dict):
        raise TrueNASAuthError(
            f'Unexpected login reply from TrueNAS: {reply!r}'
        )
    return cast('dict[str, Any]', reply)


def _login_plain(session: Session, username: str, api_key: str) -> None:
    reply = _reply_dict(
        session.call(
            'auth.login_ex',
            {
                'mechanism': 'API_KEY_PLAIN',
                'username': username,
                'api_key': api_key,
            },
        )
    )
    response_type = reply.get('response_type')
    if response_type != 'SUCCESS':
        raise TrueNASAuthError(_rejection(response_type, reply, username))


def offered_mechanisms(session: Session) -> list[str]:
    """Mechanisms TrueNAS offers (``auth.mechanism_choices``).

    Servers without the method, and 25.04.2 through 25.10.2 where calling
    it before login fails, offer only the plain API-key login.
    """
    try:
        result = session.call('auth.mechanism_choices')
    except TrueNASApiError as exc:
        if exc.errno == jsonrpc.ENOMETHOD or 'may_create_auth_token' in str(
            exc
        ):
            return ['API_KEY_PLAIN']
        raise
    if not isinstance(result, list):
        return []
    return [str(item) for item in cast('list[object]', result)]


def _split_key(api_key: str) -> tuple[int, str]:
    match = _API_KEY.match(api_key)
    if match is None:
        raise TrueNASAuthError(
            'The API key is not in the "<id>-<key>" form TrueNAS issues, '
            'which SCRAM needs.'
        )
    return int(match.group(1)), match.group(2)


def _scram_step(
    session: Session, step: str, message: str, expected: str, user: str
) -> str:
    reply = _reply_dict(
        session.call(
            'auth.login_ex',
            {'mechanism': 'SCRAM', 'scram_type': step, 'rfc_str': message},
        )
    )
    response_type = reply.get('response_type')
    if response_type != 'SCRAM_RESPONSE':
        raise TrueNASAuthError(_rejection(response_type, reply, user))
    rfc_str = reply.get('rfc_str')
    if reply.get('scram_type') != expected or not isinstance(rfc_str, str):
        raise TrueNASAuthError(
            f'Unexpected SCRAM reply from TrueNAS: {reply.get("scram_type")!r}'
        )
    return rfc_str


def _login_scram(
    session: Session,
    username: str,
    api_key: str,
    channel_binding: bool | None,
) -> None:
    if 'SCRAM' not in offered_mechanisms(session):
        raise TrueNASAuthError(
            'TrueNAS does not offer SCRAM login, which needs TrueNAS 26 or '
            'later. Set truenas_auth_mechanism = PLAIN to log in with the '
            'key itself.'
        )
    if not username:
        raise TrueNASAuthError('SCRAM login needs truenas_login.')
    key_id, secret = _split_key(api_key)

    binding: bytes | None = None
    if channel_binding is not False:
        cert = session.peer_certificate()
        if cert is not None:
            try:
                binding = scram.tls_server_end_point(cert)
            except scram.ScramError as exc:
                raise TrueNASAuthError(str(exc)) from exc
        elif channel_binding:
            raise TrueNASAuthError(
                'SCRAM channel binding needs a TLS (wss://) connection.'
            )

    try:
        client = scram.ScramClient(
            username, key_id, secret, channel_binding=binding
        )
        server_first = _scram_step(
            session,
            'CLIENT_FIRST_MESSAGE',
            client.client_first(),
            'SERVER_FIRST_RESPONSE',
            username,
        )
        server_final = _scram_step(
            session,
            'CLIENT_FINAL_MESSAGE',
            client.client_final(server_first),
            'SERVER_FINAL_RESPONSE',
            username,
        )
        client.verify_server_final(server_final)
    except scram.ScramError as exc:
        # TrueNAS may have accepted the proof; never keep a session with a
        # server that failed to prove itself.
        try:
            session.call('auth.logout')
        except Exception:  # pragma: no cover - best effort
            pass
        raise TrueNASAuthError(f'SCRAM login failed: {exc}') from exc
