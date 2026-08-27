"""Kalshi request signing.  RSA-PSS over `timestamp_ms + METHOD + path`.

research/03: no login, no session, no JWT.  Every request carries three headers:

    KALSHI-ACCESS-KEY         the key id from the dashboard
    KALSHI-ACCESS-SIGNATURE   base64 RSA-PSS(SHA-256) signature
    KALSHI-ACCESS-TIMESTAMP   Unix milliseconds

Two things that silently break this:
  * the signed path EXCLUDES the query string
  * PSS salt length must equal the digest length (not the max)

Clock skew produces mysterious 401s -- NTP-discipline the host (PLAN.md 6.x).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

KEY_HEADER = "KALSHI-ACCESS-KEY"
SIG_HEADER = "KALSHI-ACCESS-SIGNATURE"
TS_HEADER = "KALSHI-ACCESS-TIMESTAMP"


def load_private_key(path: str | Path, *, password: bytes | None = None) -> RSAPrivateKey:
    """Load a PKCS#8 PEM private key downloaded from the Kalshi dashboard."""
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"expected an RSA private key, got {type(key).__name__}")
    return key


def sign_message(key: RSAPrivateKey, message: str) -> str:
    """RSA-PSS / SHA-256 with salt length == digest length, base64-encoded."""
    signature = key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def signing_string(timestamp_ms: int, method: str, path: str) -> str:
    """The exact string Kalshi signs.

    The query string is EXCLUDED -- sign only the path.  Getting this wrong
    yields a 401 that looks like a credential problem.
    """
    return f"{timestamp_ms}{method.upper()}{path.split('?')[0]}"


@dataclass(frozen=True, slots=True)
class KalshiSigner:
    """Produces the three auth headers for a REST call or WS handshake."""

    key_id: str
    private_key: RSAPrivateKey

    @classmethod
    def from_file(cls, key_id: str, key_path: str | Path,
                  *, password: bytes | None = None) -> "KalshiSigner":
        return cls(key_id=key_id, private_key=load_private_key(key_path, password=password))

    def headers(self, method: str, path: str,
                *, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        msg = signing_string(ts, method, path)
        return {
            KEY_HEADER: self.key_id,
            SIG_HEADER: sign_message(self.private_key, msg),
            TS_HEADER: str(ts),
        }

    def ws_headers(self, ws_path: str = "/trade-api/ws/v2",
                   *, timestamp_ms: int | None = None) -> dict[str, str]:
        """The WebSocket handshake signs `timestamp + "GET" + ws_path`."""
        return self.headers("GET", ws_path, timestamp_ms=timestamp_ms)


def verify_signature(public_key: rsa.RSAPublicKey, message: str, signature_b64: str) -> bool:
    """Verify a signature.  Used only by tests -- Kalshi verifies in production."""
    from cryptography.exceptions import InvalidSignature

    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False
