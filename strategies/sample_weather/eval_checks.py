"""sample_weather/eval_checks.py — this tenant's eval-gate sections (manifest hook `eval_checks`).

Run by `make eval-fast` after the core framework gate, labeled `sample_weather:<section>`.
OFFLINE by design: pure math + static-table assertions, no store, no network — the gate must stay
green on a fresh clone and in CI.
"""
from __future__ import annotations

from desk.eval.api import Section

from .cities import CITIES, bucket_prob, event_slug_of, parse_bucket, parse_slug


def check_bucket_math() -> Section:
    """HARD guard: slug/bucket parsing and the gaussian bucket integral — the two places this sample
    could silently price the wrong interval."""
    s = Section("bucket_math", hard=True)
    try:
        spec = parse_slug("highest-temperature-in-london-on-october-18-2026-17c")
        s.check(spec is not None and spec["city"] == "london" and spec["date"] == "2026-10-18"
                and spec["metric"] == "highest", "canonical slug parses", repr(spec))
        s.check(spec["lo_c"] == 16.5 and spec["hi_c"] == 17.5,
                "integer bucket = nearest-degree rounding interval [16.5, 17.5)", repr(spec))
        s.check(parse_bucket("16-17c") == (15.5, 17.5), "range bucket widens both edges")
        s.check(parse_bucket("35corbelow") == (None, 35.5), "open-below bucket")
        s.check(parse_bucket("32corhigher") == (31.5, None), "open-above bucket")
        lo, hi = parse_bucket("90f")
        s.check(abs(lo - (89.5 - 32) * 5 / 9) < 1e-9 and abs(hi - (90.5 - 32) * 5 / 9) < 1e-9,
                "°F bucket: rounding edges built in °F, then converted", f"({lo},{hi})")
        s.check(parse_slug("will-the-fed-cut-rates-2026") is None, "non-temperature slug → None")
        s.check(parse_slug("highest-temperature-in-atlantis-on-may-1-2026-20c") is None,
                "unknown city → None (skip, never guess)")
        # gaussian: a full ladder of buckets must integrate to ~1, with symmetric tails
        edges = [None] + [x + 0.5 for x in range(-20, 61)] + [None]
        total = sum(bucket_prob(17.3, 2.0, edges[i], edges[i + 1]) for i in range(len(edges) - 1))
        s.check(abs(total - 1.0) < 1e-9, "bucket probabilities over a full ladder sum to 1", f"{total}")
        s.check(abs(bucket_prob(17.0, 2.0, None, 17.0) - 0.5) < 1e-12, "CDF symmetry at the mean")
        s.check(bucket_prob(17.0, 2.0, 20.0, 18.0) == 0.0, "inverted bucket clamps to 0")
    except Exception as e:
        s.check(False, "bucket-math guard ran", repr(e))
    return s


def check_city_table() -> Section:
    """HARD guard: the static city table is well-formed (coords in range, real IANA tz, σ>0) and the
    event hook strips exactly the bucket — a malformed entry would skip or mis-group its city."""
    s = Section("city_table", hard=True)
    try:
        from zoneinfo import ZoneInfo
        bad = []
        for k, c in CITIES.items():
            try:
                ZoneInfo(c["tz"])
                ok = (-90 <= c["lat"] <= 90) and (-180 <= c["lon"] <= 180) and c["sigma_c"] > 0
            except Exception:
                ok = False
            if not ok:
                bad.append(k)
        s.check(not bad, f"{len(CITIES)} city entries valid (lat/lon range, IANA tz, σ>0)", repr(bad))
        s.check(event_slug_of("highest-temperature-in-london-on-october-18-2026-17c")
                == "highest-temperature-in-london-on-october-18-2026",
                "event_slug_of strips only the trailing bucket (keeps the year)")
        s.check(event_slug_of("highest-temperature-in-nyc-on-july-9-2026-90-91f")
                == "highest-temperature-in-nyc-on-july-9-2026", "…including °F range buckets")
        odd = "some-future-slug-shape-2026"
        s.check(event_slug_of(odd) == odd, "unrecognized shape returned unchanged (own-event fallback)")
    except Exception as e:
        s.check(False, "city-table guard ran", repr(e))
    return s


def sections():
    return [check_bucket_math(), check_city_table()]
