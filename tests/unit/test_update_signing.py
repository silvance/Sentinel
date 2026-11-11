"""Ed25519 for update files: RFC 8032 test vectors, an independent implementation, and refusals."""

from __future__ import annotations

import pytest

from sentinel.update import ed25519

RFC = [  # RFC 8032 section 7.1, TEST 1 and TEST 2
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe"
     "24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302a"
     "eeb00d291612bb0c00"),
]


@pytest.mark.parametrize("sk,pk,msg,sig", RFC)
def test_rfc8032_vectors(sk: str, pk: str, msg: str, sig: str) -> None:
    secret, message = bytes.fromhex(sk), bytes.fromhex(msg)
    assert ed25519.public_key(secret).hex() == pk
    assert ed25519.sign(secret, message).hex() == sig
    assert ed25519.verify(bytes.fromhex(pk), message, bytes.fromhex(sig))


def test_agrees_with_an_independent_implementation() -> None:
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    for i in range(5):
        secret = ed25519.new_private_key()
        msg = bytes(range(i * 37 % 256)) * (i + 1)
        ours = ed25519.sign(secret, msg)
        theirs = crypto.Ed25519PrivateKey.from_private_bytes(secret)
        assert ours == theirs.sign(msg)
        theirs.public_key().verify(ours, msg)                    # raises if invalid
        assert ed25519.verify(ed25519.public_key(secret), msg, ours)


def test_refuses_changed_message_signature_key_and_bad_input() -> None:
    secret = ed25519.new_private_key()
    pub, msg = ed25519.public_key(secret), b'{"version": "0.3.1"}'
    sig = ed25519.sign(secret, msg)
    assert ed25519.verify(pub, msg, sig)
    assert not ed25519.verify(pub, msg + b" ", sig)
    assert not ed25519.verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not ed25519.verify(ed25519.public_key(ed25519.new_private_key()), msg, sig)
    assert not ed25519.verify(pub, msg, sig[:63]) and not ed25519.verify(pub[:31], msg, sig)
    s = int.from_bytes(sig[32:], "little") + ed25519._L                # same signature, non-canonical S
    assert not ed25519.verify(pub, msg, sig[:32] + s.to_bytes(32, "little"))
    assert not ed25519.verify(b"\xff" * 32, msg, sig)                  # not a point
