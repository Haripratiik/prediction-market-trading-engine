"""Harvest the open Kalshi market universe from the public API and characterize it.

No authentication required for market data. Polite paging with backoff.
Writes:  research/recon/kalshi_markets.json.gz   (raw)
         research/recon/kalshi_events.json.gz    (raw, nested markets)
Run:     python research/recon/harvest_kalshi.py
"""
from __future__ import annotations
import gzip, json, os, ssl, sys, time, urllib.error, urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
OUT = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context()
SLEEP = 0.25          # polite; unauthenticated public endpoint
MAX_PAGES = 400


def get(path: str, params: dict | None = None, tries: int = 5):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "pm-research/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=45, context=CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                print(f"  HTTP {e.code}, backoff {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except Exception as e:  # transient network
            wait = 2 ** attempt
            print(f"  {type(e).__name__}, backoff {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed after {tries}: {url}")


import urllib.parse  # noqa: E402  (used in get)


def page_all(path: str, key: str, params: dict, max_pages: int = MAX_PAGES):
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        d = get(path, p)
        batch = d.get(key, []) or []
        out.extend(batch)
        cursor = d.get("cursor")
        pages += 1
        print(f"  page {pages:>3}  +{len(batch):>5}  total={len(out):>7}", file=sys.stderr)
        if not cursor or not batch:
            break
        time.sleep(SLEEP)
    return out, (cursor is not None and pages >= max_pages)


def main():
    # /markets is dominated by multivariate parlay shards; /events with nested markets
    # is the real universe AND carries mutually_exclusive + settlement_sources.
    markets, truncated_m = [], False

    print("Harvesting open events (nested) ...", file=sys.stderr)
    events, truncated_e = page_all(
        "/events", "events", {"limit": 200, "status": "open", "with_nested_markets": "true"}, max_pages=400
    )
    print(f"events={len(events)} truncated={truncated_e}", file=sys.stderr)

    meta = {
        "harvested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": BASE,
        "n_markets": len(markets),
        "n_events": len(events),
        "markets_truncated": truncated_m,
        "events_truncated": truncated_e,
    }
    with gzip.open(os.path.join(OUT, "kalshi_markets.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"meta": meta, "markets": markets}, f)
    with gzip.open(os.path.join(OUT, "kalshi_events.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"meta": meta, "events": events}, f)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
