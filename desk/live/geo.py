"""live/geo.py — PRE-FLIGHT geo guard for real-money execution.

Polymarket geoblocks by the IP of the request. A VPN exit in a BLOCKED country
(e.g. Germany) => orders rejected AND a ToS §2.1.4 violation that can FREEZE funds.
This checks the bot's actual EGRESS country before any order and FAILS CLOSED
(refuse if blocked OR undetermined). The executor calls geo_gate() before trading.

Run standalone (do this with your VPN in whatever state you'll trade in):
    python3 -m desk.live.geo
If it prints your actual (allowed) location, the bot egresses where you expect.
If it prints a blocked country, your VPN is full-tunnel — split-tunnel it or turn it off.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

# Restricted jurisdictions (ISO-3166-alpha-2). NOT exhaustive and CHANGES often —
# Polymarket's own geoblock is authoritative; this is a conservative pre-flight guard.
RESTRICTED = {
    "US", "GB", "FR", "DE", "IT", "BE", "PL", "PT", "HU", "AR",   # US + EU/UK gambling + recent bans
    "SG", "TW", "TH", "AU",                                       # APAC restrictions
    "CU", "IR", "KP", "SY", "RU", "BY", "VE",                     # OFAC / sanctioned
}
TIMEOUT = 8


@dataclass
class GeoResult:
    ok: bool
    ip: Optional[str]
    country: Optional[str]
    place: str
    reason: str


def _lookup() -> tuple[Optional[str], Optional[str], str]:
    """(ip, ISO country, human place) from public geo-IP services; None if undetermined.
    Retries across MULTIPLE services over 2 rounds — a transient blip on one fire must not fail-closed the
    whole trade (2026-07-10: 24/24 overnight fires blocked on egress=None while manual checks passed 5/5).
    Semantics UNCHANGED: returns None (-> fail-closed) only if EVERY attempt across every service fails."""
    # HTTPS services first; plain-HTTP ip-api.com LAST-RESORT ONLY — an unauthenticated cleartext
    # answer feeding a fail-closed MONEY gate is on-path-spoofable, so it must never outrank an
    # HTTPS source (it stays at all only because a transient all-HTTPS outage otherwise blocks
    # every trade; see 2026-07-10 egress=None incident).
    services = (
        ("https://ipinfo.io/json", "ip", "country", "city"),
        ("https://ifconfig.co/json", "ip", "country_iso", "city"),
        ("https://api.country.is", "ip", "country", None),
        ("http://ip-api.com/json", "query", "countryCode", "country"),
    )
    for _round in range(2):
        for url, ipk, ck, namek in services:
            try:
                j = requests.get(url, timeout=TIMEOUT).json()
                ctry = (j.get(ck) or "").upper()
                if ctry:
                    place = ", ".join(x for x in ((j.get(namek) if namek else None), j.get("region"), ctry) if x)
                    return j.get(ipk), ctry, place or ctry
            except Exception:
                continue
    return None, None, ""


_GATE_CACHE = {"ts": 0.0, "res": None}
GATE_CACHE_S = 60.0     # the adapter probes per ORDER on top of the executor's per-run probe —
#   up to 8 orders × 8s timeouts with SIGALRM disarmed; one fresh probe per minute bounds that.


def geo_gate() -> GeoResult:
    """Fail-closed geo check for real-money trading. A PASSING result is cached ~60s (an egress
    doesn't flap second-to-second and per-order re-probes added up to minutes of blocking HTTP in
    the live path); a BLOCK/undetermined result is NEVER cached — fail-closed stays fresh and
    recovery (VPN off) takes effect on the very next probe."""
    now = time.time()
    c = _GATE_CACHE["res"]
    if c is not None and c.ok and now - _GATE_CACHE["ts"] < GATE_CACHE_S:
        return c
    ip, country, place = _lookup()
    if not country:
        return GeoResult(False, ip, None, place, "could not determine egress country (fail-closed)")
    if country in RESTRICTED:
        return GeoResult(False, ip, country, place,
                         f"egress country {country} is RESTRICTED — VPN/geoblock; refusing (would freeze funds)")
    res = GeoResult(True, ip, country, place, f"egress {country} not in restricted list")
    _GATE_CACHE["ts"], _GATE_CACHE["res"] = now, res
    return res


if __name__ == "__main__":
    r = geo_gate()
    print(f"egress IP:      {r.ip}")
    print(f"egress place:   {r.place}")
    print(f"country:        {r.country}")
    print(f"geo_gate:       {'PASS ✅' if r.ok else 'BLOCK ⛔'} — {r.reason}")
    if not r.ok:
        print("\n>>> Do NOT trade. If this shows a VPN country, split-tunnel your VPN or turn it off,")
        print(">>> then re-run until it shows your actual (allowed) location.")
