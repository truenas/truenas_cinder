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
"""Unit tests for logging in to TrueNAS with an API key."""

from __future__ import annotations

import ssl
import unittest
from typing import Any, cast

from tests.unit import certificates, fakes
from truenas_cinder import auth, jsonrpc, scram
from truenas_cinder.exception import TrueNASApiError, TrueNASAuthError

API_KEY = '7-q9Lr0Xb2d7Hf4Kp1Tz6Vw3Ny8Cs5Mj0A'
SECRET = API_KEY.split('-', 1)[1]


class _FakeTrueNAS:
    """A session with a fake TrueNAS: answers the login methods and records
    every call."""

    def __init__(
        self,
        *,
        mechanisms: object = ('API_KEY_PLAIN', 'SCRAM'),
        plain_reply: object = None,
        server: fakes.FakeScramServer | None = None,
        certificate: str | None = None,
    ) -> None:
        self.mechanisms = mechanisms
        self.plain_reply = (
            plain_reply
            if plain_reply is not None
            else {'response_type': 'SUCCESS'}
        )
        self.server = server or fakes.FakeScramServer(
            'truenas_admin', 7, SECRET
        )
        self.certificate = (
            ssl.PEM_cert_to_DER_cert(certificate) if certificate else None
        )
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.forge_server_signature = False

    def call(self, method: str, *params: object) -> object:
        self.calls.append((method, params))
        if method == 'auth.mechanism_choices':
            if isinstance(self.mechanisms, BaseException):
                raise self.mechanisms
            return list(cast('tuple[str, ...]', self.mechanisms))
        if method == 'auth.logout':
            return True
        assert method == 'auth.login_ex'
        request = cast('dict[str, Any]', params[0])
        if request['mechanism'] == 'API_KEY_PLAIN':
            return self.plain_reply
        return self._scram(request)

    def _scram(self, request: dict[str, Any]) -> object:
        message = request['rfc_str']
        try:
            if request['scram_type'] == 'CLIENT_FIRST_MESSAGE':
                rfc, step = self.server.first(message), 'SERVER_FIRST_RESPONSE'
            else:
                rfc, step = self.server.final(message), 'SERVER_FINAL_RESPONSE'
                if self.forge_server_signature:
                    rfc = 'v=' + 'A' * 88
        except (ValueError, KeyError):
            return {'response_type': 'AUTH_ERR'}
        return {
            'response_type': 'SCRAM_RESPONSE',
            'scram_type': step,
            'rfc_str': rfc,
        }

    def peer_certificate(self) -> bytes | None:
        return self.certificate

    def login_requests(self) -> list[dict[str, Any]]:
        return [
            cast('dict[str, Any]', params[0])
            for method, params in self.calls
            if method == 'auth.login_ex'
        ]


def _login(
    session: _FakeTrueNAS, mechanism: str = 'PLAIN', **kwargs: Any
) -> None:
    auth.login(session, 'truenas_admin', API_KEY, mechanism, **kwargs)


class PlainLoginTest(unittest.TestCase):
    def test_sends_username_and_key(self) -> None:
        session = _FakeTrueNAS()
        _login(session, 'plain')  # case-insensitive
        self.assertEqual(
            session.calls,
            [
                (
                    'auth.login_ex',
                    (
                        {
                            'mechanism': 'API_KEY_PLAIN',
                            'username': 'truenas_admin',
                            'api_key': API_KEY,
                        },
                    ),
                )
            ],
        )

    def test_rejections_explain_themselves(self) -> None:
        cases = {
            'AUTH_ERR': "belongs to the user 'truenas_admin'",
            'EXPIRED': 'has expired',
            'DENIED': 'not allowed to use the API',
            'SOMETHING_NEW': "'SOMETHING_NEW'",
        }
        for response_type, expected in cases.items():
            with self.subTest(response_type):
                session = _FakeTrueNAS(
                    plain_reply={'response_type': response_type}
                )
                with self.assertRaises(TrueNASAuthError) as ctx:
                    _login(session)
                self.assertIn(expected, str(ctx.exception))

    def test_redirect_names_the_controller(self) -> None:
        session = _FakeTrueNAS(
            plain_reply={
                'response_type': 'REDIRECT',
                'urls': ['wss://controller-b/api/current'],
            }
        )
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session)
        self.assertIn('controller-b', str(ctx.exception))

    def test_unexpected_reply(self) -> None:
        session = _FakeTrueNAS(plain_reply=['not', 'a', 'dict'])
        self.assertRaises(TrueNASAuthError, _login, session)

    def test_missing_key_and_unknown_mechanism(self) -> None:
        session = _FakeTrueNAS()
        self.assertRaises(
            TrueNASAuthError,
            auth.login,
            session,
            'truenas_admin',
            '',
            'PLAIN',
        )
        self.assertRaises(TrueNASAuthError, _login, session, 'MAGIC')
        self.assertEqual(session.calls, [])


class ScramLoginTest(unittest.TestCase):
    def test_unbound_without_tls(self) -> None:
        session = _FakeTrueNAS()
        _login(session, 'SCRAM')
        first, final = session.login_requests()
        self.assertEqual(first['scram_type'], 'CLIENT_FIRST_MESSAGE')
        self.assertTrue(first['rfc_str'].startswith('n,,n=truenas_admin:7,'))
        self.assertEqual(final['scram_type'], 'CLIENT_FINAL_MESSAGE')

    def test_the_key_is_never_sent(self) -> None:
        session = _FakeTrueNAS()
        _login(session, 'SCRAM')
        for method, params in session.calls:
            self.assertNotIn(SECRET, repr(params), method)

    def test_bound_to_the_tls_certificate(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(certificates.RSA_SHA256)
        session = _FakeTrueNAS(
            certificate=certificates.RSA_SHA256,
            server=fakes.FakeScramServer(
                'truenas_admin',
                7,
                SECRET,
                expected_binding=scram.tls_server_end_point(der),
            ),
        )
        _login(session, 'SCRAM')
        first = session.login_requests()[0]
        self.assertTrue(first['rfc_str'].startswith('p=tls-server-end-point'))

    def test_man_in_the_middle_is_rejected(self) -> None:
        # We see the attacker's certificate; TrueNAS binds to its own.
        der = ssl.PEM_cert_to_DER_cert(certificates.RSA_SHA256)
        session = _FakeTrueNAS(
            certificate=certificates.EC_SHA384,
            server=fakes.FakeScramServer(
                'truenas_admin',
                7,
                SECRET,
                expected_binding=scram.tls_server_end_point(der),
            ),
        )
        self.assertRaises(TrueNASAuthError, _login, session, 'SCRAM')

    def test_binding_can_be_turned_off(self) -> None:
        session = _FakeTrueNAS(certificate=certificates.RSA_SHA256)
        _login(session, 'SCRAM', channel_binding=False)
        self.assertTrue(
            session.login_requests()[0]['rfc_str'].startswith('n,,')
        )

    def test_required_binding_needs_tls(self) -> None:
        session = _FakeTrueNAS()
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session, 'SCRAM', channel_binding=True)
        self.assertIn('wss://', str(ctx.exception))

    def test_certificate_without_a_binding_hash(self) -> None:
        session = _FakeTrueNAS(certificate=certificates.ED25519)
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session, 'SCRAM')
        self.assertIn('not defined', str(ctx.exception))

    def test_wrong_key(self) -> None:
        session = _FakeTrueNAS(
            server=fakes.FakeScramServer('truenas_admin', 7, 'other-key')
        )
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session, 'SCRAM')
        self.assertIn('rejected the API key', str(ctx.exception))

    def test_server_that_cannot_prove_itself_is_logged_out(self) -> None:
        session = _FakeTrueNAS()
        session.forge_server_signature = True
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session, 'SCRAM')
        self.assertIn('could not prove', str(ctx.exception))
        self.assertEqual(session.calls[-1][0], 'auth.logout')

    def test_never_falls_back_to_plain(self) -> None:
        # TrueNAS's list is unauthenticated: an attacker could strip SCRAM
        # from it, and falling back would hand over the key.
        session = _FakeTrueNAS(mechanisms=('API_KEY_PLAIN',))
        with self.assertRaises(TrueNASAuthError) as ctx:
            _login(session, 'SCRAM')
        self.assertIn('truenas_auth_mechanism = PLAIN', str(ctx.exception))
        self.assertEqual(session.login_requests(), [])

    def test_servers_without_the_mechanism_list(self) -> None:
        missing = TrueNASApiError(
            'auth.mechanism_choices failed: Method not found',
            errno=jsonrpc.ENOMETHOD,
        )
        # TrueNAS 25.04.2 through 25.10.2 fail this call before login.
        broken = TrueNASApiError(
            "auth.mechanism_choices failed: 'NoneType' object has no "
            "attribute 'may_create_auth_token'"
        )
        for error in (missing, broken):
            with self.subTest(str(error)):
                session = _FakeTrueNAS(mechanisms=error)
                self.assertEqual(
                    auth.offered_mechanisms(session), ['API_KEY_PLAIN']
                )
                self.assertRaises(TrueNASAuthError, _login, session, 'SCRAM')

    def test_other_mechanism_list_errors_propagate(self) -> None:
        session = _FakeTrueNAS(mechanisms=TrueNASApiError('boom'))
        self.assertRaises(TrueNASApiError, _login, session, 'SCRAM')

    def test_key_must_have_an_id(self) -> None:
        session = _FakeTrueNAS()
        with self.assertRaises(TrueNASAuthError) as ctx:
            auth.login(session, 'truenas_admin', 'no-id-here', 'SCRAM')
        self.assertIn('"<id>-<key>"', str(ctx.exception))
        self.assertEqual(session.login_requests(), [])


if __name__ == '__main__':
    unittest.main()
