"""Verify Kalshi credentials end to end.  Run this before anything authenticated.

    python -m scripts.check_auth

Checks, in order:
  1. credentials are configured and the key file exists
  2. the private key loads and is a real RSA key
  3. a signature verifies locally (catches a corrupt or truncated download)
  4. the clock is not skewed (skew produces 401s that look like bad credentials)
  5. an AUTHENTICATED call succeeds against the configured environment

Never prints key material.
"""

from __future__ import annotations

import sys
import time

import httpx

from core.config import load_settings
from venues.kalshi.auth import SIG_HEADER, signing_string, verify_signature
from venues.kalshi.client import KalshiClient, KalshiError

OK, BAD, WARN = "  [ok]", "  [FAIL]", "  [warn]"


def main() -> int:
    settings = load_settings()
    creds = settings.kalshi
    print("=" * 70)
    print("KALSHI CREDENTIAL CHECK")
    print("=" * 70)
    print(f"  environment : {creds.env}")
    print(f"  status      : {creds.describe()}")

    if not creds.is_complete:
        print(f"\n{BAD} credentials incomplete. Set these (config/secrets.env or env vars):")
        print("      KALSHI_ENV=demo")
        print("      KALSHI_KEY_ID=<uuid from the dashboard>")
        print("      KALSHI_PRIVATE_KEY_PATH=<path to the .pem you downloaded>")
        return 1

    # 2 -- key loads
    try:
        signer = creds.signer()
        bits = signer.private_key.key_size
        print(f"{OK} private key loaded ({bits}-bit RSA)")
        if bits < 2048:
            print(f"{WARN} expected 2048-bit; got {bits}")
    except Exception as exc:
        print(f"{BAD} could not load the private key: {type(exc).__name__}: {exc}")
        print("      If it was downloaded once and saved wrong, generate a new key pair.")
        return 1

    # 3 -- signature verifies locally
    msg = signing_string(1700000000000, "GET", "/trade-api/v2/portfolio/balance")
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance",
                             timestamp_ms=1700000000000)
    if verify_signature(signer.private_key.public_key(), msg, headers[SIG_HEADER]):
        print(f"{OK} signature verifies locally")
    else:
        print(f"{BAD} signature did not verify -- the key file may be corrupt")
        return 1

    # 4 -- clock skew
    try:
        r = httpx.get("https://api.elections.kalshi.com/trade-api/v2/exchange/status",
                      timeout=15)
        server_date = r.headers.get("date")
        if server_date:
            from email.utils import parsedate_to_datetime

            skew = abs(time.time() - parsedate_to_datetime(server_date).timestamp())
            if skew < 5:
                print(f"{OK} clock skew {skew:.1f}s")
            else:
                print(f"{WARN} clock skew {skew:.1f}s -- run `w32tm /resync`; "
                      "skew causes 401s that look like credential errors")
    except Exception:
        print(f"{WARN} could not measure clock skew")

    # 5 -- a real authenticated call
    print(f"\n  calling {creds.base_url}/portfolio/balance ...")
    with KalshiClient(base_url=creds.base_url, signer=signer) as client:
        try:
            bal = client.balance()
            print(f"{OK} AUTHENTICATED. balance response: {bal}")
        except KalshiError as exc:
            print(f"{BAD} {exc}")
            if exc.status == 401:
                print("      401 usually means: key not yet active, wrong environment "
                      "(demo key against prod or vice versa), or clock skew.")
            return 1

        try:
            fills = client.fills(limit=1)
            print(f"{OK} /portfolio/fills reachable ({len(fills.get('fills', []))} rows)")
        except KalshiError as exc:
            print(f"{WARN} fills endpoint: {exc}")

    print("\n  Ready. Next: T-011 WebSocket recorder, T-045 order lifecycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
