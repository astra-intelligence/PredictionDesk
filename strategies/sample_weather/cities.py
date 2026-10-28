"""sample_weather/cities.py — static city table, slug parsing, gaussian bucket math.

CITY TABLE HONESTY: coordinates are CITY-CENTER approximations, not the official settlement
station each market resolves on; `sigma_c` is a HAND-SET constant (°C) standing in for a real
forecast-error calibration. Both are deliberate tutorial simplifications — a serious strategy
derives the station from the market's resolution source and calibrates σ against observed errors.

Slug grammar (as captured from Polymarket's daily temperature ladders):
  (highest|lowest)-temperature-in-<city>-on-<month>-<day>-<year>-<bucket>
Bucket suffix forms and their °C intervals (resolution ROUNDS to the nearest whole degree in the
market's native unit, so the integer bucket "17c" covers actual temps in [16.5, 17.5)):
  "17c"           -> [16.5, 17.5)
  "16-17c"        -> [15.5, 17.5)
  "35corbelow"    -> (-inf, 35.5)
  "32corhigher"   -> [31.5, +inf)
  "90f" etc.      -> the same shapes in °F; BOUNDS are built in the native unit, then converted to °C
"""
from __future__ import annotations

import math
import re
from typing import Optional, Tuple

# ~10 of the cities the default capture lane actually records (slug key -> location + fixed σ).
# tz = IANA zone of the city: Open-Meteo aggregates the daily max/min over this LOCAL day, matching
# how the markets define "the highest temperature on <date>".
CITIES = {
    "london":       {"lat": 51.51,  "lon": -0.13,   "tz": "Europe/London",     "sigma_c": 2.0},
    "amsterdam":    {"lat": 52.37,  "lon": 4.90,    "tz": "Europe/Amsterdam",  "sigma_c": 2.0},
    "paris":        {"lat": 48.86,  "lon": 2.35,    "tz": "Europe/Paris",      "sigma_c": 2.2},
    "istanbul":     {"lat": 41.01,  "lon": 28.95,   "tz": "Europe/Istanbul",   "sigma_c": 2.2},
    "nyc":          {"lat": 40.78,  "lon": -73.97,  "tz": "America/New_York",  "sigma_c": 2.5},
    "seoul":        {"lat": 37.57,  "lon": 126.98,  "tz": "Asia/Seoul",        "sigma_c": 2.2},
    "tokyo":        {"lat": 35.68,  "lon": 139.77,  "tz": "Asia/Tokyo",        "sigma_c": 1.8},
    "shanghai":     {"lat": 31.23,  "lon": 121.47,  "tz": "Asia/Shanghai",     "sigma_c": 2.0},
    "taipei":       {"lat": 25.04,  "lon": 121.56,  "tz": "Asia/Taipei",       "sigma_c": 1.6},
    "sao-paulo":    {"lat": -23.55, "lon": -46.63,  "tz": "America/Sao_Paulo", "sigma_c": 2.4},
    "cape-town":    {"lat": -33.92, "lon": 18.42,   "tz": "Africa/Johannesburg", "sigma_c": 2.2},
    "buenos-aires": {"lat": -34.60, "lon": -58.38,  "tz": "America/Argentina/Buenos_Aires", "sigma_c": 2.4},
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

_SLUG_RE = re.compile(
    r"^(?P<metric>highest|lowest)-temperature-in-"
    r"(?P<city>[a-z-]+?)-on-"
    r"(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})-"
    r"(?P<bucket>.+)$")

_BUCKET_RE = re.compile(
    r"^(?P<a>\d+)(?:-(?P<b>\d+))?(?P<unit>c|f)"
    r"(?P<qual>orbelow|orless|orhigher|orabove|ormore)?$")

# trailing temperature bucket, e.g. "...-2026-17c" / "...-2026-61-62f" / "...-2026-32corhigher"
_EVENT_RE = re.compile(r"(-\d{4})-\d+(?:-\d+)?[cf](?:orbelow|orless|orhigher|orabove|ormore)?$")


def event_slug_of(market_slug: str) -> str:
    """Manifest hook `event_slug_of`: the EVENT key that groups every bucket of one city-day, for the
    framework's per-event exposure cap. Strips ONLY the trailing temperature bucket (keeps the year),
    matching Polymarket's eventSlug. An unrecognized shape is returned UNCHANGED — the market then
    counts as its own event, so the cap still binds per-market (fail-closed, never pools)."""
    return _EVENT_RE.sub(r"\1", market_slug or "")


def discover_event_slugs(window_days: int) -> list:
    """Manifest hook `discover_event_slugs(window_days) -> [slug]`: enumerate this tenant's daily
    temperature-event slugs for today..today+window_days, from the city table above. The slug
    grammar is TENANT knowledge — the capture layer fetches whatever slugs a tenant declares and
    owns none of the grammar itself."""
    import datetime
    today = datetime.date.today()
    cities = sorted(CITIES)
    out = []
    for d in range(int(window_days) + 1):
        day = today + datetime.timedelta(days=d)
        mon, dom, yr = day.strftime("%B").lower(), day.day, day.year
        for metric in ("highest", "lowest"):
            for city in cities:
                out.append(f"{metric}-temperature-in-{city}-on-{mon}-{dom}-{yr}")
    return out


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def parse_bucket(suffix: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """(lo_c, hi_c) for a bucket suffix; None side = open. None if unparseable. Bounds sit at the
    .5 rounding edges IN THE NATIVE UNIT (a °F market rounds in °F), then convert to °C."""
    m = _BUCKET_RE.match(suffix or "")
    if not m:
        return None
    a = float(m.group("a"))
    b = float(m.group("b")) if m.group("b") else None
    qual = m.group("qual")
    conv = _f_to_c if m.group("unit") == "f" else (lambda x: x)
    if qual in ("orbelow", "orless"):
        lo, hi = None, conv(a + 0.5)
    elif qual in ("orhigher", "orabove", "ormore"):
        lo, hi = conv(a - 0.5), None
    elif b is not None:
        lo, hi = conv(a - 0.5), conv(b + 0.5)
    else:
        lo, hi = conv(a - 0.5), conv(a + 0.5)
    return lo, hi


def parse_slug(slug: str) -> Optional[dict]:
    """{'metric': 'highest'|'lowest', 'city', 'date': 'YYYY-MM-DD', 'lo_c', 'hi_c'} or None
    (not a temperature market / unknown city / unparseable bucket — skip, never guess)."""
    m = _SLUG_RE.match(slug or "")
    if not m:
        return None
    city = m.group("city")
    if city not in CITIES:
        return None
    month = _MONTHS.get(m.group("month"))
    if not month:
        return None
    bucket = parse_bucket(m.group("bucket"))
    if bucket is None:
        return None
    return {"metric": m.group("metric"), "city": city,
            "date": f"{int(m.group('year')):04d}-{month:02d}-{int(m.group('day')):02d}",
            "lo_c": bucket[0], "hi_c": bucket[1]}


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_prob(mu_c: float, sigma_c: float, lo_c: Optional[float], hi_c: Optional[float]) -> float:
    """P(temp ∈ [lo_c, hi_c)) under N(mu_c, sigma_c²). Open sides integrate to the tail."""
    hi = _phi((hi_c - mu_c) / sigma_c) if hi_c is not None else 1.0
    lo = _phi((lo_c - mu_c) / sigma_c) if lo_c is not None else 0.0
    return max(0.0, min(1.0, hi - lo))
