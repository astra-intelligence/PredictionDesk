"""capture_config.json -> Gamma /markets -> token universe -> _universe.json.

Resolves a CaptureConfig into the concrete list of (market_id, token_id) to record.
Selection = explicit_slugs (override) OR per-tag top-by-volume markets, filtered by
volume + resolution window, deduped, capped at max_markets.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from desk.types import CaptureConfig


def _get(url: str, params: Optional[dict] = None, timeout: float = 20.0) -> object:
    r = requests.get(url, params=params, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _tag_id(cfg: CaptureConfig, slug: str) -> Optional[str]:
    try:
        t = _get(f"{cfg.gamma_url}/tags/slug/{slug}")
        return str(t.get("id")) if isinstance(t, dict) and t.get("id") is not None else None
    except Exception as e:  # noqa: BLE001
        print(f"[select] tag '{slug}' lookup failed: {e}")
        return None


def _parse_token_ids(market: dict) -> List[str]:
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in ids]
    except (ValueError, TypeError):
        return []


def _within_window(market: dict, end_within_days: Optional[int]) -> bool:
    if end_within_days is None:
        return True
    end_iso = market.get("endDateIso") or market.get("endDate")
    if not end_iso:
        return True
    try:
        # endDate is a date or ISO datetime; compare by date string prefix vs cutoff
        import datetime as dt
        end = dt.datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        cutoff = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=end_within_days)
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        return end <= cutoff
    except (ValueError, TypeError):
        return True


def _markets_for_tag(cfg: CaptureConfig, tag_id: str, max_pages: int = 1) -> List[dict]:
    """Volume-ordered markets for a tag, paginated + per-page error-tolerant (a 429 on one page
    doesn't abort the whole selection). max_pages>1 lets a lane scan deep enough to find matches
    after include/date filters (a busy tag can have 1000+ markets)."""
    out: List[dict] = []
    off = 0
    for _ in range(max_pages):
        try:
            d = _get(f"{cfg.gamma_url}/markets",
                     {"closed": "false", "active": "true", "tag_id": str(tag_id),
                      "order": "volume24hr", "ascending": "false", "limit": "100", "offset": str(off)})
        except Exception:  # noqa: BLE001 — skip this page, keep what we have
            break
        if not isinstance(d, list) or not d:
            break
        out += [m for m in d if isinstance(m, dict)]
        if len(d) < 100:
            break
        off += 100
    return out


def _lane_pages(lane: dict) -> int:
    # deep pagination when filters scatter matches across volume-ordered pages; else shallow
    if lane.get("end_within_days") is not None or lane.get("include_substrings"):
        return 25
    return max(1, int(lane.get("count", 0)) // 100 + 1)


# ── date-discovery lanes: tenant-owned slug enumeration ──────────────────────────────────────
# The TENANT owns the event-slug grammar (manifest hook `discover_event_slugs(window_days) ->
# [slug]`); the framework keeps only the generic half: per-slug Gamma event fetch, dedupe, lane
# filters, window rotation. DEPRECATED alias: `date_discovery_cities` (tenant hook or lane key)
# wraps a city list through the legacy daily-temperature template below — byte-identical slugs to
# the pre-hook behavior, so existing tenants keep working unchanged while they migrate.
_LEGACY_DATE_TEMPLATE = "{metric}-temperature-in-{city}-on-{mon}-{dom}-{yr}"   # DEPRECATED-alias only


def _legacy_city_slugs(cities, metrics, days: int, today=None) -> List[str]:
    """DEPRECATED-alias enumeration: cities × metrics × window dates through the legacy
    temperature-event template (day → metric → city, the historical fetch order)."""
    import datetime
    today = today or datetime.date.today()   # daemon context: real date auto-rotates the window
    out: List[str] = []
    for d in range(int(days) + 1):
        day = today + datetime.timedelta(days=d)
        mon, dom, yr = day.strftime("%B").lower(), day.day, day.year
        for metric in metrics:
            for city in cities:
                out.append(_LEGACY_DATE_TEMPLATE.format(metric=metric, city=city, mon=mon, dom=dom, yr=yr))
    return out


def _discovery_slugs(lane: dict, hooks: Optional[tuple] = None, today=None) -> List[str]:
    """The slug list for a `date_discovery` lane, deduped in contribution order.

    Sources, composed across tenants:
      • manifest hook `discover_event_slugs(window_days) -> [slug]` — used verbatim (the tenant
        owns the grammar); a tenant declaring BOTH hooks contributes through this one only.
      • DEPRECATED alias — a lane-config `date_discovery_cities` list (precedence, as before) or
        else the union of tenant `date_discovery_cities` hooks, wrapped through the legacy
        temperature template with the lane's `date_discovery_metrics`.
    `hooks` = ({tenant: discover_fn}, {tenant: cities_fn}) injection point for the eval gate;
    None = registry discovery. A failing hook is LOUDLY skipped, never aborting the others."""
    days = int(lane.get("date_discovery_days", lane.get("end_within_days", 5)))
    metrics = lane.get("date_discovery_metrics") or ["highest", "lowest"]
    if hooks is None:
        try:
            from desk.signals import registry as _R
            hooks = (dict(_R.hooks_for("discover_event_slugs")),
                     dict(_R.hooks_for("date_discovery_cities")))
        except Exception as e:  # noqa: BLE001
            print(f"[select] date_discovery: tenant hook discovery failed ({e!r})", flush=True)
            hooks = ({}, {})
    new_hooks, old_hooks = hooks
    slugs: List[str] = []
    for tname in sorted(new_hooks):
        try:
            slugs += [str(x) for x in new_hooks[tname](days)]
        except Exception as e:  # noqa: BLE001
            print(f"[select] tenant '{tname}' discover_event_slugs failed ({e!r}) — skipped", flush=True)
    lane_cities = lane.get("date_discovery_cities")
    if lane_cities:
        print("[select] date_discovery: lane key 'date_discovery_cities' is DEPRECATED — move the "
              "slug grammar into a tenant 'discover_event_slugs' hook", flush=True)
        slugs += _legacy_city_slugs(lane_cities, metrics, days, today)
    else:
        cities: set = set()
        for tname in sorted(old_hooks):
            if tname in new_hooks:
                continue                     # the tenant already migrated — new hook wins
            print(f"[select] tenant '{tname}' hook 'date_discovery_cities' is DEPRECATED — declare "
                  f"'discover_event_slugs(window_days)' instead (the tenant owns the slug grammar)",
                  flush=True)
            try:
                cities |= set(old_hooks[tname]())
            except Exception as e:  # noqa: BLE001
                print(f"[select] tenant '{tname}' date_discovery_cities failed ({e!r}) — skipped", flush=True)
        if cities:
            slugs += _legacy_city_slugs(sorted(cities), metrics, days, today)
    if not slugs:
        # graceful absence: no tenant hook + no lane list -> the lane discovers nothing, loudly.
        print("[select] date_discovery: no slugs (no tenant discovery hook, no lane city list) — "
              "lane discovers nothing", flush=True)
    seen: set = set()
    return [x for x in slugs if not (x in seen or seen.add(x))]


def _date_discovery_markets(cfg: CaptureConfig, lane: dict) -> List[dict]:
    """Tag-INDEPENDENT discovery for daily event ladders. Polymarket lists each day's ladder (e.g.
    'highest/lowest temperature in <city> on <Month> <D>') as an EVENT whose bucket markets are
    frequently UNTAGGED for the first day+ (2026-07-09: london-jul-10 had $3.5k volume but tag_ids=[]),
    so the tag query MISSES them and the scanner starves the moment the current day resolves.
    We instead ENUMERATE the event slugs across the lane window (tenant-owned grammar, see
    _discovery_slugs) and pull each event's markets directly — every in-window date is captured
    from listing, auto-updating with the date each run."""
    out: List[dict] = []
    seen: set = set()
    for slug in _discovery_slugs(lane):
        try:
            ev = _get(f"{cfg.gamma_url}/events", {"slug": slug})
        except Exception:          # a single missing/empty event must not abort discovery
            continue
        e = ev[0] if isinstance(ev, list) and ev else (ev if isinstance(ev, dict) else None)
        if not e:
            continue
        for m in (e.get("markets") or []):
            c = m.get("conditionId")
            if c and c not in seen and not m.get("closed"):
                seen.add(c)
                out.append(m)
    return out


def _lane_match(cfg: CaptureConfig, lane: dict, m: dict) -> bool:
    """All per-lane filters: keep + volume floor + exclude/include substrings + resolution window."""
    q = (m.get("question") or "").lower()
    if not _keep(cfg, m):
        return False
    if float(m.get("volume24hr") or 0) < float(lane.get("min_volume24hr", cfg.min_volume24hr)):
        return False
    excl = [s.lower() for s in lane.get("exclude_substrings", [])]
    if excl and any(s in q for s in excl):
        return False
    incl = [s.lower() for s in lane.get("include_substrings", [])]
    if incl and not any(s in q for s in incl):
        return False
    ewd = lane.get("end_within_days")
    if ewd is not None and not _within_window(m, int(ewd)):
        return False
    return True


def _select_lanes(cfg: CaptureConfig) -> List[tuple]:
    """Lane-weighted selection. Returns [(market, lane_name)] deduped across lanes.

    Per-lane exclude_substrings drop unwanted markets; global cap_patterns limit
    near-identical families (e.g. Elon-tweet bands) across all lanes.
    """
    seen_cond = set()
    out: List[tuple] = []
    caps = [(cp["substring"].lower(), int(cp["max"])) for cp in (cfg.cap_patterns or [])]
    cap_count: Dict[str, int] = {}

    def over_cap(q: str) -> bool:
        ql = q.lower()
        return any(sub in ql and cap_count.get(sub, 0) >= mx for sub, mx in caps)

    def bump(q: str) -> None:
        ql = q.lower()
        for sub, _ in caps:
            if sub in ql:
                cap_count[sub] = cap_count.get(sub, 0) + 1

    for m in _markets_for_slugs(cfg, cfg.explicit_slugs):
        c = m.get("conditionId")
        if _keep(cfg, m) and c not in seen_cond:
            seen_cond.add(c)
            out.append((m, "explicit"))
            bump(m.get("question") or "")

    for lane in cfg.lanes:
        name = lane.get("name", "lane")
        count = int(lane.get("count", 0))
        pages = _lane_pages(lane)
        pool: dict = {}
        for tid in lane.get("tag_ids", []):
            for m in _markets_for_tag(cfg, tid, max_pages=pages):
                c = m.get("conditionId")
                if c and c not in seen_cond and c not in pool and _lane_match(cfg, lane, m):
                    pool[c] = m
        if lane.get("date_discovery"):                  # tag-INDEPENDENT daily event ladders (untagged
            for m in _date_discovery_markets(cfg, lane):     # fresh markets -> tag query misses them; #capture)
                c = m.get("conditionId")
                if c and c not in seen_cond and c not in pool and _lane_match(cfg, lane, m):
                    pool[c] = m
        # balance_by_end_date: volume-DESC ranking structurally STARVES the newest target day —
        # freshly listed markets have ~0 volume until traded, so a pure volume cut always picks
        # them up late and near-dated coverage never exists from listing moment. Round-robin
        # one-per-end-day (nearest day first, volume-ranked within a day) so every in-window day
        # gets coverage as soon as it lists.
        if lane.get("balance_by_end_date"):
            by_day: Dict[str, list] = {}
            for m in sorted(pool.values(), key=lambda m: float(m.get("volume24hr") or 0), reverse=True):
                by_day.setdefault((m.get("endDate") or "9999")[:10], []).append(m)
            days = sorted(by_day)
            ranked = []
            while any(by_day[d] for d in days):
                for d in days:
                    if by_day[d]:
                        ranked.append(by_day[d].pop(0))
        else:
            ranked = sorted(pool.values(), key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
        taken = 0
        for m in ranked:
            if taken >= count:
                break
            q = m.get("question") or ""
            if over_cap(q):
                continue
            seen_cond.add(m.get("conditionId"))
            out.append((m, name))
            bump(q)
            taken += 1
    return out


def _markets_for_slugs(cfg: CaptureConfig, slugs: List[str]) -> List[dict]:
    out = []
    for s in slugs:
        try:
            m = _get(f"{cfg.gamma_url}/markets/slug/{s}")
            if isinstance(m, list):
                out.extend(m)
            elif isinstance(m, dict):
                out.append(m)
        except Exception as e:  # noqa: BLE001
            print(f"[select] slug '{s}' failed: {e}")
    return out


def _keep(cfg: CaptureConfig, m: dict) -> bool:
    return (bool(m.get("conditionId")) and m.get("enableOrderBook", True)
            and float(m.get("volume24hr") or 0) >= cfg.min_volume24hr
            and _within_window(m, cfg.end_within_days) and bool(_parse_token_ids(m)))


def select_universe(cfg: CaptureConfig) -> Dict:
    """Return {'token_id': {market_id, token_id, slug, question, volume24hr, tag}, ...}.

    Lane-weighted (cfg.lanes) if provided; else per-tag round-robin. explicit_slugs always included.
    """
    if cfg.lanes:
        universe: Dict[str, dict] = {}
        for m, lane in _select_lanes(cfg):
            cond = m.get("conditionId")
            for tok in _parse_token_ids(m):
                universe[tok] = {
                    "market_id": cond, "token_id": tok, "slug": m.get("slug"),
                    "question": m.get("question"), "volume24hr": float(m.get("volume24hr") or 0),
                    "tag": lane,
                }
        return universe

    tag_of: Dict[str, str] = {}
    per_tag: Dict[str, List[dict]] = {}
    if cfg.explicit_slugs:
        ms = [m for m in _markets_for_slugs(cfg, cfg.explicit_slugs) if _keep(cfg, m)]
        for m in ms:
            tag_of[m.get("slug")] = "explicit"
        per_tag["explicit"] = sorted(ms, key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
    else:
        for slug in (cfg.tags or []):
            tid = _tag_id(cfg, slug)
            if not tid:
                continue
            ms = [m for m in _markets_for_tag(cfg, tid) if _keep(cfg, m)]
            for m in ms:
                tag_of.setdefault(m.get("slug"), slug)
            per_tag[slug] = sorted(ms, key=lambda m: float(m.get("volume24hr") or 0), reverse=True)

    # round-robin across tags until max_markets, dedup by conditionId
    seen_cond = set()
    filtered: List[dict] = []
    idx = {t: 0 for t in per_tag}
    while len(filtered) < cfg.max_markets and any(idx[t] < len(per_tag[t]) for t in per_tag):
        for t in per_tag:
            if len(filtered) >= cfg.max_markets:
                break
            while idx[t] < len(per_tag[t]):
                m = per_tag[t][idx[t]]
                idx[t] += 1
                if m.get("conditionId") not in seen_cond:
                    seen_cond.add(m.get("conditionId"))
                    filtered.append(m)
                    break

    universe: Dict[str, dict] = {}
    for m in filtered:
        cond = m.get("conditionId")
        for tok in _parse_token_ids(m):
            universe[tok] = {
                "market_id": cond,
                "token_id": tok,
                "slug": m.get("slug"),
                "question": m.get("question"),
                "volume24hr": float(m.get("volume24hr") or 0),
                "tag": tag_of.get(m.get("slug"), ""),
            }
    return universe


def write_universe(cfg: CaptureConfig, universe: Dict) -> str:
    os.makedirs(cfg.out_dir, exist_ok=True)
    path = os.path.join(cfg.out_dir, "_universe.json")
    payload = {"generated_ts": int(time.time()), "n_markets": len({u["market_id"] for u in universe.values()}),
               "n_tokens": len(universe), "tokens": universe}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return path


def paced_map(fn, items: list, per_10s: int = 240, workers: int = 16) -> list:
    """Concurrent map that respects a documented per-10s rate limit: run in chunks of ~80% of the
    limit, sleeping 10s between chunks. Keeps us under Gamma /markets (300/10s) and Data /trades (200/10s)."""
    out: list = []
    chunk = max(1, int(per_10s * 0.8))
    for i in range(0, len(items), chunk):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            out += list(ex.map(fn, items[i:i + chunk]))
        if i + chunk < len(items):
            time.sleep(10)
    return out


def market_by_slug(cfg: CaptureConfig, slug: Optional[str]) -> Optional[dict]:
    if not slug:
        return None
    try:
        m = _get(f"{cfg.gamma_url}/markets/slug/{slug}")
        if isinstance(m, list):
            return m[0] if m else None
        return m if isinstance(m, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _resolution_rows(cond: str, meta: dict, mkt: dict) -> List[dict]:
    """Build Resolution-schema rows (T0 truth) from a closed Gamma market.
    payout = 1.0 for the winning token, 0.0 for losers (from outcomePrices).

    BINARY-ONLY: a row is emitted only when the token's outcomePrice is exactly 0.0 or 1.0 —
    a merely-closed market (prices missing/None) or a not-yet-reported one (0.5/0.5) must NOT
    enter resolutions.jsonl, where a local-first settle would book it confidently wrong. Skipped
    tokens are counted + logged; the market stays unsettled until Gamma reports a real winner
    (store falls through to the Gamma/CLOB chain meanwhile)."""
    toks = _parse_token_ids(mkt) or _parse_token_ids(meta)
    raw_p = mkt.get("outcomePrices")
    raw_o = mkt.get("outcomes")
    try:
        prices = json.loads(raw_p) if isinstance(raw_p, str) else (raw_p or [])
        outs = json.loads(raw_o) if isinstance(raw_o, str) else (raw_o or [])
    except (ValueError, TypeError):
        prices, outs = [], []
    rows = []
    skipped = 0
    for i, tk in enumerate(toks):
        try:
            payout = float(prices[i]) if i < len(prices) and prices[i] not in (None, "") else None
        except (TypeError, ValueError):
            payout = None
        if payout not in (0.0, 1.0):
            skipped += 1
            continue
        rows.append({
            "market_id": cond, "token_id": str(tk), "resolved_ts": int(time.time()),
            "payout": payout,
            "outcome": outs[i] if i < len(outs) else None, "source": "GAMMA",
            "question": meta.get("question") or mkt.get("question"),
        })
    if skipped:
        print(f"[select] {cond[:16]}…: {skipped} token(s) skipped from resolutions.jsonl "
              f"(outcomePrices not an exact 0/1 — closed but not reported; will settle later)")
    return rows


def refresh_universe(cfg: CaptureConfig, current: Dict[str, dict]) -> Dict:
    """STICKY refresh: keep every still-open market (no churn from volume dips), drop only genuinely
    resolved/closed markets (recording their resolution = T0 truth), and backfill just those freed
    slots with new open markets in the SAME lane. Maintains each lane's target count.

    Returns {universe, resolutions, resolved, added, removed}. Collector applies `universe`, appends
    `resolutions` to resolutions.jsonl.
    """
    cur_by_cond: Dict[str, dict] = {}
    for m in current.values():
        cur_by_cond.setdefault(m["market_id"], m)
    conds = list(cur_by_cond)

    # 1. check each current market's live state — PACED under Gamma /markets 300/10s (~25s for 506)
    pairs = paced_map(lambda c: (c, market_by_slug(cfg, cur_by_cond[c].get("slug"))), conds, per_10s=240)
    states: Dict[str, Optional[dict]] = dict(pairs)

    kept: set = set()
    resolutions, resolved = [], []
    for cond, meta in cur_by_cond.items():
        mkt = states.get(cond)
        if mkt is not None and (mkt.get("closed") or mkt.get("umaResolutionStatus") == "resolved"):
            resolved.append(cond)
            resolutions += _resolution_rows(cond, meta, mkt)
        else:
            kept.add(cond)  # open, or transient fetch failure (don't drop on a blip)

    new_uni: Dict[str, dict] = {tok: m for tok, m in current.items() if m["market_id"] in kept}

    # 2. backfill freed slots per lane with new open markets (same category), to the lane's count
    kept_by_lane: Dict[str, int] = {}
    for cond in kept:
        kept_by_lane[cur_by_cond[cond]["tag"]] = kept_by_lane.get(cur_by_cond[cond]["tag"], 0) + 1
    seen = set(kept) | set(resolved)
    for lane in cfg.lanes:
        name = lane.get("name", "lane")
        need = int(lane.get("count", 0)) - kept_by_lane.get(name, 0)
        if need <= 0:
            continue
        pages = _lane_pages(lane)
        pool: dict = {}
        for tid in lane.get("tag_ids", []):
            for m in _markets_for_tag(cfg, tid, max_pages=pages):
                c = m.get("conditionId")
                if c and c not in seen and c not in pool and _lane_match(cfg, lane, m):
                    pool[c] = m
        if lane.get("date_discovery"):                  # tag-INDEPENDENT daily event ladders (#capture)
            for m in _date_discovery_markets(cfg, lane):
                c = m.get("conditionId")
                if c and c not in seen and c not in pool and _lane_match(cfg, lane, m):
                    pool[c] = m
        for m in sorted(pool.values(), key=lambda x: float(x.get("volume24hr") or 0), reverse=True)[:need]:
            seen.add(m["conditionId"])
            for tok in _parse_token_ids(m):
                new_uni[tok] = {"market_id": m["conditionId"], "token_id": tok, "slug": m.get("slug"),
                                "question": m.get("question"), "volume24hr": float(m.get("volume24hr") or 0),
                                "tag": name}
    new_conds = {u["market_id"] for u in new_uni.values()}
    return {
        "universe": new_uni, "resolutions": resolutions, "resolved": resolved,
        "added": sorted(new_conds - set(cur_by_cond)),
        "removed": sorted(set(cur_by_cond) - new_conds),
    }


if __name__ == "__main__":
    import sys
    from desk.runtime.paths import repo_path, resolve_data_rel
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else repo_path("desk", "capture_config.json")
    cfg = CaptureConfig.from_json(json.load(open(cfg_path)))
    cfg.out_dir = resolve_data_rel(cfg.out_dir)   # a relative config out_dir must never follow cwd
    uni = select_universe(cfg)
    p = write_universe(cfg, uni)
    mkts = {u["market_id"]: u for u in uni.values()}
    print(f"[select] {len(mkts)} markets / {len(uni)} tokens -> {p}")
    for u in list(mkts.values()):
        print(f"  {u['tag']:>8} vol24h={u['volume24hr']:>12.0f}  {(u['question'] or '')[:64]}")
