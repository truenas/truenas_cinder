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
"""Unit tests for SCRAM-SHA-512 and tls-server-end-point channel binding.

Exchanges run against ``fakes.FakeScramServer``, an independent server-side
implementation, so the client is not checked against itself.
"""

from __future__ import annotations

import base64
import hashlib
import ssl
import unittest

from tests.unit import certificates, fakes
from truenas_cinder import scram

SECRET = 'q9Lr0Xb2d7Hf4Kp1Tz6Vw3Ny8Cs5Mj0A'


def _der(pem: str) -> bytes:
    return ssl.PEM_cert_to_DER_cert(pem)


def _exchange(
    client: scram.ScramClient, server: fakes.FakeScramServer
) -> None:
    server_first = server.first(client.client_first())
    server_final = server.final(client.client_final(server_first))
    client.verify_server_final(server_final)


class SaslprepTest(unittest.TestCase):
    def test_ascii_is_unchanged(self) -> None:
        self.assertEqual(scram.saslprep('truenas_admin'), 'truenas_admin')

    def test_mappings_and_normalization(self) -> None:
        # Non-ASCII space becomes a space; a soft hyphen is removed;
        # NFKC folds the fullwidth letter.
        self.assertEqual(scram.saslprep('a b­cＡ'), 'a bcA')

    def test_prohibited_characters(self) -> None:
        self.assertRaises(scram.ScramError, scram.saslprep, 'bad\u0007')

    def test_mixed_directions(self) -> None:
        self.assertRaises(scram.ScramError, scram.saslprep, 'אa')
        self.assertEqual(scram.saslprep('אב'), 'אב')


class ChannelBindingTest(unittest.TestCase):
    def test_hash_follows_the_certificate_signature(self) -> None:
        for pem, expected in certificates.EXPECTED_HASHES.items():
            der = _der(pem)
            with self.subTest(expected=expected):
                if expected is None:
                    self.assertRaises(
                        scram.ScramError, scram.tls_server_end_point, der
                    )
                    continue
                self.assertEqual(scram.certificate_hash_name(der), expected)
                self.assertEqual(
                    scram.tls_server_end_point(der),
                    hashlib.new(expected, der).digest(),
                )

    def test_malformed_certificates(self) -> None:
        der = _der(certificates.RSA_SHA256)
        for bad in (b'', b'\x30', b'\x30\x05abc', der[:40], b'\x02\x01\x00'):
            with self.subTest(bad=bad[:8]):
                self.assertRaises(
                    scram.ScramError, scram.certificate_hash_name, bad
                )


class ExchangeTest(unittest.TestCase):
    def _client(self, binding: bytes | None = None) -> scram.ScramClient:
        return scram.ScramClient(
            'truenas_admin', 7, SECRET, channel_binding=binding
        )

    def _server(self, binding: bytes | None = None) -> fakes.FakeScramServer:
        return fakes.FakeScramServer(
            'truenas_admin', 7, SECRET, expected_binding=binding
        )

    def test_unbound_exchange(self) -> None:
        client = self._client()
        first = client.client_first()
        self.assertTrue(first.startswith('n,,n=truenas_admin:7,r='))
        nonce = base64.b64decode(first.rsplit('r=', 1)[1])
        self.assertEqual(len(nonce), scram.NONCE_SIZE)
        _exchange(client, self._server())

    def test_bound_exchange(self) -> None:
        binding = scram.tls_server_end_point(_der(certificates.RSA_SHA256))
        client = self._client(binding)
        self.assertTrue(
            client.client_first().startswith('p=tls-server-end-point,,')
        )
        _exchange(client, self._server(binding))

    def test_binding_to_another_certificate_fails(self) -> None:
        # A man in the middle presents its own certificate.
        ours = scram.tls_server_end_point(_der(certificates.RSA_SHA256))
        theirs = scram.tls_server_end_point(_der(certificates.EC_SHA384))
        client = self._client(theirs)
        server = self._server(ours)
        final = client.client_final(server.first(client.client_first()))
        self.assertRaises(ValueError, server.final, final)

    def test_wrong_key_fails(self) -> None:
        client = scram.ScramClient('truenas_admin', 7, 'not-the-key')
        server = self._server()
        final = client.client_final(server.first(client.client_first()))
        self.assertRaises(ValueError, server.final, final)

    def test_server_that_does_not_know_the_key_is_rejected(self) -> None:
        client = self._client()
        server = self._server()
        final = client.client_final(server.first(client.client_first()))
        signature = bytearray(base64.b64decode(server.final(final)[2:]))
        signature[0] ^= 1
        forged = 'v=' + base64.b64encode(bytes(signature)).decode()
        with self.assertRaises(scram.ScramError) as ctx:
            client.verify_server_final(forged)
        self.assertIn('could not prove', str(ctx.exception))

    def test_server_error_message(self) -> None:
        client = self._client()
        server = self._server()
        client.client_final(server.first(client.client_first()))
        with self.assertRaises(scram.ScramError) as ctx:
            client.verify_server_final('e=invalid-proof')
        self.assertIn('invalid-proof', str(ctx.exception))

    def test_verify_needs_the_final_message_first(self) -> None:
        self.assertRaises(
            scram.ScramError, self._client().verify_server_final, 'v=AAAA'
        )

    def test_rejects_bad_server_first_messages(self) -> None:
        client = self._client()
        ours = base64.b64decode(client.client_first().rsplit('r=', 1)[1])
        good_nonce = base64.b64encode(ours + b'S' * 32).decode()
        foreign = base64.b64encode(b'X' * 64).decode()
        salt = base64.b64encode(b'salt').decode()
        bad = {
            'nonce not ours': f'r={foreign},s={salt},i=50000',
            'no server part': (
                f'r={base64.b64encode(ours).decode()},s={salt},i=50000'
            ),
            'too few iterations': f'r={good_nonce},s={salt},i=4096',
            'too many iterations': f'r={good_nonce},s={salt},i=9999999',
            'iterations not a number': f'r={good_nonce},s={salt},i=lots',
            'salt not base64': f'r={good_nonce},s=!!!,i=50000',
            'empty salt': f'r={good_nonce},s=,i=50000',
            'unknown attribute': f'r={good_nonce},s={salt},i=50000,m=x',
            'missing attribute': f'r={good_nonce},s={salt}',
            'duplicate attribute': f'r={good_nonce},r={good_nonce},i=50000',
        }
        for label, message in bad.items():
            with self.subTest(label):
                self.assertRaises(
                    scram.ScramError, client.client_final, message
                )

    def test_rejects_usernames_scram_cannot_carry(self) -> None:
        for name in ('a,b', 'a=b'):
            with self.subTest(name=name):
                self.assertRaises(
                    scram.ScramError, scram.ScramClient, name, 7, SECRET
                )


if __name__ == '__main__':
    unittest.main()
