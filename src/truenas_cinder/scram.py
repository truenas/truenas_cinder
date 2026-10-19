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
"""SCRAM-SHA-512 for TrueNAS API keys.

SCRAM (RFC 5802) proves the client knows the API key without sending it,
and makes TrueNAS prove the same in return. TrueNAS's API-key variant:

- the user is sent as ``<username>:<api key id>``;
- the secret is the part of the key after ``<id>-``, used as is (no
  SASLprep);
- nonces are raw bytes, so the server's nonce is the base64 of
  ``client nonce || server nonce`` rather than a text extension of ours;
- channel binding is ``tls-server-end-point`` (RFC 5929): a hash of the
  server's TLS certificate is folded into the proof, so a man in the middle
  that terminates TLS with its own certificate cannot complete the login.

Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import stringprep
import unicodedata

#: Bytes of client nonce, as TrueNAS expects.
NONCE_SIZE = 32
#: Iteration counts TrueNAS's SCRAM implementation accepts.
MIN_ITERATIONS = 50_000
MAX_ITERATIONS = 5_000_000
MAX_SALT_SIZE = 1024
#: The channel-binding type TrueNAS supports.
TLS_SERVER_END_POINT = 'tls-server-end-point'


class ScramError(ValueError):
    """The SCRAM exchange failed or a message was malformed."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _unb64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScramError(f'{what} is not valid base64.') from exc


def saslprep(text: str) -> str:
    """Prepare a username with the SASLprep profile (RFC 4013).

    Unassigned code points are allowed, as RFC 5802 asks for usernames.
    """
    mapped = ''.join(
        ' ' if stringprep.in_table_c12(ch) else ch
        for ch in text
        if not stringprep.in_table_b1(ch)
    )
    prepared = unicodedata.normalize('NFKC', mapped)
    prohibited = (
        stringprep.in_table_c12,
        stringprep.in_table_c21_c22,
        stringprep.in_table_c3,
        stringprep.in_table_c4,
        stringprep.in_table_c5,
        stringprep.in_table_c6,
        stringprep.in_table_c7,
        stringprep.in_table_c8,
        stringprep.in_table_c9,
    )
    for ch in prepared:
        if any(check(ch) for check in prohibited):
            raise ScramError(
                f'The username contains a prohibited character '
                f'(U+{ord(ch):04X}).'
            )
    if any(stringprep.in_table_d1(ch) for ch in prepared):
        # Right-to-left text must be all right-to-left at both ends.
        if any(stringprep.in_table_d2(ch) for ch in prepared) or not (
            stringprep.in_table_d1(prepared[0])
            and stringprep.in_table_d1(prepared[-1])
        ):
            raise ScramError('The username mixes text directions.')
    return prepared


# --- tls-server-end-point (RFC 5929) ------------------------------------

# Hash of each certificate signature algorithm, by OID. MD5 and SHA-1 are
# replaced by SHA-256 afterwards, as RFC 5929 section 4.1 requires.
_SIGNATURE_HASHES = {
    '1.2.840.113549.1.1.4': 'md5',  # md5WithRSAEncryption
    '1.2.840.113549.1.1.5': 'sha1',  # sha1WithRSAEncryption
    '1.2.840.113549.1.1.14': 'sha224',  # sha224WithRSAEncryption
    '1.2.840.113549.1.1.11': 'sha256',  # sha256WithRSAEncryption
    '1.2.840.113549.1.1.12': 'sha384',  # sha384WithRSAEncryption
    '1.2.840.113549.1.1.13': 'sha512',  # sha512WithRSAEncryption
    '1.2.840.10045.4.1': 'sha1',  # ecdsa-with-SHA1
    '1.2.840.10045.4.3.1': 'sha224',  # ecdsa-with-SHA224
    '1.2.840.10045.4.3.2': 'sha256',  # ecdsa-with-SHA256
    '1.2.840.10045.4.3.3': 'sha384',  # ecdsa-with-SHA384
    '1.2.840.10045.4.3.4': 'sha512',  # ecdsa-with-SHA512
    '1.2.840.10040.4.3': 'sha1',  # id-dsa-with-sha1
    '2.16.840.1.101.3.4.3.1': 'sha224',  # id-dsa-with-sha224
    '2.16.840.1.101.3.4.3.2': 'sha256',  # id-dsa-with-sha256
}
# RSASSA-PSS names its hash in the algorithm parameters instead.
_RSASSA_PSS = '1.2.840.113549.1.1.10'
_HASH_ALGORITHMS = {
    '1.2.840.113549.2.5': 'md5',
    '1.3.14.3.2.26': 'sha1',
    '2.16.840.1.101.3.4.2.4': 'sha224',
    '2.16.840.1.101.3.4.2.1': 'sha256',
    '2.16.840.1.101.3.4.2.2': 'sha384',
    '2.16.840.1.101.3.4.2.3': 'sha512',
}

_SEQUENCE = 0x30
_OID = 0x06
_CONTEXT_0 = 0xA0


def _der_item(data: bytes, pos: int, tag: int) -> tuple[int, int]:
    """(start, end) of the content of the DER item at ``pos``.

    Raises ``ScramError`` unless that item has the expected ``tag``.
    """
    if pos + 2 > len(data) or data[pos] != tag:
        raise ScramError('The TrueNAS certificate could not be parsed.')
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        size = length & 0x7F
        if not 0 < size <= 4 or pos + size > len(data):
            raise ScramError('The TrueNAS certificate could not be parsed.')
        length = int.from_bytes(data[pos : pos + size], 'big')
        pos += size
    if pos + length > len(data):
        raise ScramError('The TrueNAS certificate could not be parsed.')
    return pos, pos + length


def _oid(content: bytes) -> str:
    values: list[int] = []
    value = 0
    for byte in content:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            values.append(value)
            value = 0
    if not values:
        raise ScramError('The TrueNAS certificate could not be parsed.')
    first = min(values[0] // 40, 2)
    arcs = [first, values[0] - 40 * first, *values[1:]]
    return '.'.join(str(arc) for arc in arcs)


def _algorithm(data: bytes, pos: int) -> tuple[str, int, int]:
    """OID of the AlgorithmIdentifier at ``pos``, and its parameters span."""
    start, end = _der_item(data, pos, _SEQUENCE)
    oid_start, oid_end = _der_item(data, start, _OID)
    return _oid(data[oid_start:oid_end]), oid_end, end


def _pss_hash(data: bytes, start: int, end: int) -> str:
    """Hash named by RSASSA-PSS parameters (RFC 4055); SHA-1 by default."""
    if start == end:
        return 'sha1'
    params_start, params_end = _der_item(data, start, _SEQUENCE)
    if params_start == params_end or data[params_start] != _CONTEXT_0:
        return 'sha1'
    inner_start, _ = _der_item(data, params_start, _CONTEXT_0)
    oid, _, _ = _algorithm(data, inner_start)
    name = _HASH_ALGORITHMS.get(oid)
    if name is None:
        raise ScramError(
            f'The TrueNAS certificate uses an unsupported RSASSA-PSS hash '
            f'({oid}).'
        )
    return name


def certificate_hash_name(cert_der: bytes) -> str:
    """The hash ``tls-server-end-point`` uses for this certificate."""
    cert_start, _ = _der_item(cert_der, 0, _SEQUENCE)
    _, tbs_end = _der_item(cert_der, cert_start, _SEQUENCE)
    oid, params_start, params_end = _algorithm(cert_der, tbs_end)
    if oid == _RSASSA_PSS:
        name = _pss_hash(cert_der, params_start, params_end)
    else:
        found = _SIGNATURE_HASHES.get(oid)
        if found is None:
            # e.g. Ed25519, which has no separate hash.
            raise ScramError(
                f'Channel binding is not defined for the signature '
                f'algorithm of the TrueNAS certificate ({oid}). Use an RSA '
                f'or ECDSA certificate, or turn channel binding off.'
            )
        name = found
    return 'sha256' if name in ('md5', 'sha1') else name


def tls_server_end_point(cert_der: bytes) -> bytes:
    """Channel-binding data for a server certificate (RFC 5929)."""
    return hashlib.new(certificate_hash_name(cert_der), cert_der).digest()


# --- the exchange -----------------------------------------------------------


def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.digest(key, message, 'sha512')


def _fields(message: str, names: tuple[str, ...]) -> dict[str, str]:
    """The ``k=v`` attributes of a server message, requiring exactly
    ``names`` (an unknown attribute may be a mandatory extension)."""
    fields: dict[str, str] = {}
    for part in message.split(','):
        name, sep, value = part.partition('=')
        if not sep or not value or name not in names or name in fields:
            raise ScramError(f'Unexpected SCRAM message from TrueNAS: {part}')
        fields[name] = value
    if set(fields) != set(names):
        raise ScramError('Incomplete SCRAM message from TrueNAS.')
    return fields


class ScramClient:
    """One SCRAM-SHA-512 exchange for a TrueNAS API key."""

    def __init__(
        self,
        username: str,
        api_key_id: int,
        secret: str,
        *,
        channel_binding: bytes | None = None,
        nonce: bytes | None = None,
    ) -> None:
        """``channel_binding`` is ``tls_server_end_point`` of the server's
        certificate, or None for an unbound exchange. ``nonce`` is for
        tests only."""
        if ',' in username or '=' in username:
            raise ScramError('The username must not contain "," or "=".')
        self._binding = channel_binding
        self._gs2_header = (
            f'p={TLS_SERVER_END_POINT},,'
            if channel_binding is not None
            else 'n,,'
        )
        self._secret = secret.encode('utf-8')
        self._nonce = (
            nonce if nonce is not None else secrets.token_bytes(NONCE_SIZE)
        )
        self._first_bare = (
            f'n={saslprep(username)}:{api_key_id},r={_b64(self._nonce)}'
        )
        self._server_signature: bytes | None = None

    def client_first(self) -> str:
        return self._gs2_header + self._first_bare

    def client_final(self, server_first: str) -> str:
        """The proof answering TrueNAS's first message."""
        fields = _fields(server_first, ('r', 's', 'i'))
        combined = _unb64(fields['r'], 'The SCRAM nonce')
        if len(combined) <= len(self._nonce) or not hmac.compare_digest(
            combined[: len(self._nonce)], self._nonce
        ):
            raise ScramError('TrueNAS answered with a nonce we did not send.')
        salt = _unb64(fields['s'], 'The SCRAM salt')
        if not 0 < len(salt) <= MAX_SALT_SIZE:
            raise ScramError('TrueNAS sent an invalid SCRAM salt.')
        try:
            iterations = int(fields['i'])
        except ValueError:
            raise ScramError(
                'TrueNAS sent an invalid iteration count.'
            ) from None
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            raise ScramError(
                f'TrueNAS asked for {iterations} iterations; only '
                f'{MIN_ITERATIONS} to {MAX_ITERATIONS} are accepted.'
            )

        salted = hashlib.pbkdf2_hmac('sha512', self._secret, salt, iterations)
        client_key = _hmac(salted, b'Client Key')
        stored_key = hashlib.sha512(client_key).digest()
        server_key = _hmac(salted, b'Server Key')

        binding_input = self._gs2_header.encode('ascii') + (
            self._binding or b''
        )
        without_proof = f'c={_b64(binding_input)},r={fields["r"]}'
        auth_message = (
            f'{self._first_bare},{server_first},{without_proof}'
        ).encode()
        signature = _hmac(stored_key, auth_message)
        # ClientKey XOR ClientSignature; both are 64-byte SHA-512 outputs.
        proof = (
            int.from_bytes(client_key, 'big')
            ^ int.from_bytes(signature, 'big')
        ).to_bytes(len(client_key), 'big')
        self._server_signature = _hmac(server_key, auth_message)
        return f'{without_proof},p={_b64(proof)}'

    def verify_server_final(self, server_final: str) -> None:
        """Check that TrueNAS proved it knows the key too."""
        if self._server_signature is None:
            raise ScramError('client_final() must be called first.')
        if server_final.startswith('e='):
            raise ScramError(
                f'TrueNAS rejected the SCRAM exchange: {server_final[2:]}'
            )
        fields = _fields(server_final, ('v',))
        signature = _unb64(fields['v'], 'The SCRAM server signature')
        if not hmac.compare_digest(signature, self._server_signature):
            raise ScramError(
                'TrueNAS could not prove it knows the API key: the SCRAM '
                'server signature does not match.'
            )
