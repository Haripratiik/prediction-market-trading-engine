"""T-010 acceptance (offline half): RSA-PSS signing is correct and the two
easy-to-get-wrong details are pinned."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from venues.kalshi.auth import (
    KEY_HEADER,
    SIG_HEADER,
    TS_HEADER,
    KalshiSigner,
    sign_message,
    signing_string,
    verify_signature,
)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    """A throwaway 2048-bit key -- no Kalshi account needed to test signing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def signer(key: rsa.RSAPrivateKey) -> KalshiSigner:
    return KalshiSigner(key_id="test-key-id", private_key=key)


def test_signing_string_layout():
    """timestamp + METHOD + path, concatenated with no separators."""
    assert signing_string(1700000000000, "get", "/trade-api/v2/portfolio/balance") == (
        "1700000000000GET/trade-api/v2/portfolio/balance"
    )


def test_query_string_is_excluded_from_the_signature():
    """The detail that produces a 401 looking like a credential problem."""
    with_query = signing_string(1, "GET", "/trade-api/v2/markets?limit=10&cursor=abc")
    without = signing_string(1, "GET", "/trade-api/v2/markets")
    assert with_query == without


def test_signature_verifies(key, signer):
    msg = signing_string(1700000000000, "GET", "/trade-api/v2/portfolio/balance")
    sig = sign_message(key, msg)
    assert verify_signature(key.public_key(), msg, sig)


def test_signature_is_rejected_for_a_different_message(key):
    sig = sign_message(key, signing_string(1, "GET", "/a"))
    assert not verify_signature(key.public_key(), signing_string(1, "GET", "/b"), sig)


def test_pss_is_randomised_so_two_signatures_differ(key):
    """PSS is probabilistic -- identical input must not produce identical output.

    A deterministic signature here would mean the salt length was set to zero.
    """
    msg = signing_string(1, "GET", "/x")
    assert sign_message(key, msg) != sign_message(key, msg)


def test_headers_are_complete_and_well_formed(signer):
    h = signer.headers("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000000)
    assert h[KEY_HEADER] == "test-key-id"
    assert h[TS_HEADER] == "1700000000000"
    # base64 of a 2048-bit signature decodes to exactly 256 bytes
    assert len(base64.b64decode(h[SIG_HEADER])) == 256


def test_headers_sign_the_timestamp_they_send(key, signer):
    h = signer.headers("POST", "/trade-api/v2/portfolio/events/orders", timestamp_ms=42)
    expected = signing_string(42, "POST", "/trade-api/v2/portfolio/events/orders")
    assert verify_signature(key.public_key(), expected, h[SIG_HEADER])


def test_timestamp_defaults_to_now_in_milliseconds(signer):
    import time

    before = int(time.time() * 1000)
    ts = int(signer.headers("GET", "/x")[TS_HEADER])
    after = int(time.time() * 1000)
    assert before <= ts <= after + 1000


def test_websocket_handshake_signs_the_ws_path(key, signer):
    h = signer.ws_headers(timestamp_ms=99)
    expected = signing_string(99, "GET", "/trade-api/ws/v2")
    assert verify_signature(key.public_key(), expected, h[SIG_HEADER])


def test_method_is_upcased(signer, key):
    h = signer.headers("get", "/x", timestamp_ms=7)
    assert verify_signature(key.public_key(), signing_string(7, "GET", "/x"), h[SIG_HEADER])
