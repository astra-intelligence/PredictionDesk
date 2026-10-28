"""adapters/sample_weather.py — the sample tenant's CURRENT-opportunities screen (dashboard page 4).

The reference implementation of the opportunities-adapter contract (adapters/CONTRACT.md): runs the
SAME pricing pass the sample_weather strategy runs at fire time (captured universe → slug parse →
lead window → one-model gaussian fair → live captured book) and renders every in-window market
sorted by |gap|. Tenant-specific BY DESIGN — it imports the sample_weather plugin — so it fails at
call time (not import time) on a tree without that tenant.

    python3 .claude/skills/desk-dashboard/scripts/adapters/sample_weather.py [out.html]
"""
from __future__ import annotations

import json
import os
import sys
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
if _REPO_ROOT not in sys.path:                  # standalone script invocation: repo root isn't on
    sys.path.insert(0, _REPO_ROOT)              # sys.path — bootstrap it (same pattern as ledger.py)

# the tenant is a desk PLUGIN: strategies dir on sys.path, then import by its top-level name
from desk.signals.registry import ensure_importable as _ensure_tenants
_ensure_tenants()

try:                                              # display tz: env DESK_TZ, default UTC
    from adapters import desk_tz_name
except ImportError:                               # standalone script: adapters/ itself is sys.path[0]
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from adapters import desk_tz_name             # noqa: E402

OPP_CACHE = os.path.join("dashboard", "opportunities.json")
# No QUOTA_PROBE_URL: the standard Open-Meteo forecast endpoint's free tier is generous; a failed
# fetch degrades per-city inside scan() instead of gating the whole rescan.


def _entry_threshold() -> float:
    """The shipped row's entry_threshold (what the strategy would actually trade), default 0.2."""
    try:
        cfg = json.load(open(os.path.join("desk", "signals", "strategy_config.json")))
        for r in cfg.get("strategies", []):
            if r.get("strategy", r.get("name")) == "sample_weather":
                return float((r.get("params") or {}).get("entry_threshold", 0.2))
    except Exception:
        pass
    return 0.2


def scan() -> dict:
    """Live rescan: price every in-window captured temperature market with the sample model and pair
    it with the latest captured book. Writes OPP_CACHE. Network: one Open-Meteo call per city, one
    Gamma lookup per in-window market (YES-token label, cached per process)."""
    from datetime import date, datetime, timezone

    from desk.data.store import Store
    from sample_weather.cities import CITIES, bucket_prob, parse_slug
    from sample_weather.strategy import _mu_for, _open_meteo_daily, _universe_meta, _yes_token

    store = Store()
    now = int(time.time())
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    decision_date = today.isoformat()
    thr = _entry_threshold()
    meta = _universe_meta(store)
    fc_cache: dict = {}
    seen: set = set()
    rows = []
    n_seen = 0
    for (m, tk), r in meta.items():
        slug = r.get("slug") or ""
        if m in seen:
            continue
        spec = parse_slug(slug)
        if spec is None:
            continue
        try:
            lead = (date.fromisoformat(spec["date"]) - today).days
        except ValueError:
            continue
        if not (1 <= lead <= 5):
            continue
        seen.add(m)
        n_seen += 1
        mu = _mu_for(_open_meteo_daily(spec["city"], "gfs_global", fc_cache), spec["date"], spec["metric"])
        sigma = float(CITIES[spec["city"]]["sigma_c"])
        fair = bucket_prob(mu, sigma, spec["lo_c"], spec["hi_c"]) if mu is not None else None
        yes_tok = _yes_token(slug)
        mid = ask = None
        if yes_tok is not None:
            d = store.route(m, yes_tok, now)
            mid = getattr(d, "mid", None)
            book = getattr(d, "book", None)
            ask = getattr(book, "best_ask", None) if book is not None else None
        gap = round(fair - mid, 4) if (fair is not None and mid is not None) else None
        edge = round(fair - ask, 4) if (fair is not None and ask is not None) else None
        lo, hi = spec["lo_c"], spec["hi_c"]
        bucket = (f"≤{hi - 0.5:g}°C" if lo is None else f"≥{lo + 0.5:g}°C" if hi is None
                  else f"{lo + 0.5:g}–{hi - 0.5:g}°C" if hi - lo > 1.0 else f"{lo + 0.5:g}°C")
        rows.append({"slug": slug, "city": spec["city"], "date": spec["date"],
                     "metric": spec["metric"], "bucket": bucket, "lead": lead,
                     "mu_c": round(mu, 1) if mu is not None else None, "sigma_c": sigma,
                     "fair": round(fair, 4) if fair is not None else None,
                     "mid": mid, "ask": ask, "gap": gap, "edge": edge,
                     "url": f"https://polymarket.com/market/{slug}"})
    rows.sort(key=lambda r: -(abs(r["gap"]) if r["gap"] is not None else -1))
    out = {"rows": rows, "n_in_window": n_seen,
           "n_actionable": sum(1 for r in rows if r["edge"] is not None and r["edge"] >= thr),
           "gauges": {}, "as_of": now, "decision_date": decision_date, "tz": desk_tz_name(),
           "lanes": {"paper": {"thr": thr, "model": "gfs_global + fixed-σ gaussian"}}}
    try:
        os.makedirs(os.path.dirname(OPP_CACHE), exist_ok=True)
        tmp = OPP_CACHE + ".tmp"
        json.dump(out, open(tmp, "w"))
        os.replace(tmp, OPP_CACHE)
    except Exception:
        pass
    return out


def scan_cached(max_age_s: int = 7200) -> dict:
    """The cached scan when fresh enough, else a live scan() — the build path (a build must not
    burn a rescan when the Refresh button just made one)."""
    try:
        if os.path.exists(OPP_CACHE) and time.time() - os.path.getmtime(OPP_CACHE) <= max_age_s:
            d = json.load(open(OPP_CACHE))
            d["from_cache"] = True
            return ensure_enriched(d)
    except Exception:
        pass
    return scan()


def ensure_enriched(res: dict) -> dict:
    """Contract no-op for this adapter: sample rows carry their full (one-model) breakdown already —
    there is no second-stage enrichment to backfill. Normalizes the tz field."""
    res.setdefault("tz", desk_tz_name())
    return res


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PredictionDesk — current opportunities</title>
<style>
  :root{--bg:#0c0e12;--bg2:#11141a;--line:#262d39;--ink:#e9e4d6;--ink-dim:#9aa1ad;--warm:#e0a44a;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
  body{background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Helvetica,sans-serif;max-width:1060px;margin:0 auto;padding:36px 22px}
  h1{font-weight:700;letter-spacing:-.5px;margin-bottom:2px}h1 .em{color:var(--warm)}
  .sub{color:var(--ink-dim);margin:0 0 18px;font-size:13px}
  table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12.5px}
  th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
  th{color:var(--ink-dim);font-weight:600;text-transform:uppercase;font-size:10.5px;letter-spacing:.4px}
  td.l,th.l{text-align:left} a{color:inherit}
  tr.hot td{background:rgba(224,164,74,.08)} .pos{color:#2b6}.neg{color:#c44}.dim{color:var(--ink-dim)}
  #rstat{font-size:12px;color:var(--ink-dim);margin:10px 0}
  button{background:var(--bg2);color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:6px 14px;cursor:pointer}
  button:hover{border-color:var(--warm)}
</style></head><body>
<h1>Current <span class="em">opportunities</span></h1>
<div class="sub">Every in-window temperature market priced by the SAMPLE model (one Open-Meteo model →
fixed-σ gaussian → bucket mass) against the latest captured book. The fair column claims no edge —
σ is hand-set and uncalibrated; rows where the executable edge clears the shipped row's threshold
are highlighted as what the paper strategy would actually attempt. Refresh needs the mini server
(http://localhost:8788).</div>
<button onclick="refresh()">Refresh (live rescan)</button> <span id="rstat"></span>
<div id="tbl"></div>
<script>
let OPP = /*__OPP__*/null;
const TZN=(OPP&&OPP.tz)||'UTC';
const $=s=>document.querySelector(s);
const f=(v,d=3)=>v==null?'—':(+v).toFixed(d);
function render(){
  if(!OPP||!OPP.rows||!OPP.rows.length){$('#tbl').innerHTML='<p class="dim">no scan yet — capture some books, then hit Refresh.</p>';return;}
  const thr=(OPP.lanes&&OPP.lanes.paper&&OPP.lanes.paper.thr)||0.2;
  $('#rstat').textContent=`${OPP.n_in_window} markets in window · ${OPP.n_actionable} over threshold ${thr} · as of ${new Date(OPP.as_of*1000).toLocaleString('en-GB',{timeZone:TZN})} ${TZN}`;
  let h='<table><tr><th class="l">market</th><th class="l">city</th><th>date</th><th>lead</th><th>bucket</th><th>model °C</th><th>σ</th><th>fair</th><th>mid</th><th>ask</th><th>gap</th><th>edge</th></tr>';
  for(const r of OPP.rows){
    const hot=r.edge!=null&&r.edge>=thr;
    h+=`<tr${hot?' class="hot"':''}><td class="l"><a href="${r.url}" target="_blank">${r.metric} ${r.bucket}</a></td>`+
       `<td class="l">${r.city}</td><td>${r.date}</td><td>${r.lead}d</td><td>${r.bucket}</td>`+
       `<td>${f(r.mu_c,1)}</td><td>${f(r.sigma_c,1)}</td><td>${f(r.fair)}</td><td>${f(r.mid)}</td><td>${f(r.ask)}</td>`+
       `<td class="${r.gap>0?'pos':r.gap<0?'neg':''}">${f(r.gap)}</td><td class="${hot?'pos':''}">${f(r.edge)}</td></tr>`;
  }
  $('#tbl').innerHTML=h+'</table>';
}
async function refresh(){
  $('#rstat').textContent='rescanning…';
  try{const r=await fetch('/api/opportunities');OPP=await r.json();render();}
  catch(e){$('#rstat').textContent='refresh failed: '+e;}
}
render();
if(!OPP||!OPP.rows||!OPP.rows.length){
  fetch('/api/opportunities/cached').then(r=>r.json()).then(j=>{if(j&&j.rows&&j.rows.length){OPP=j;render();}}).catch(()=>{});
}
</script></body></html>"""


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("dashboard", "opportunities.html")
    data = scan_cached()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(PAGE.replace("/*__OPP__*/null", json.dumps(data)))
    print(f"[sample_weather] {len(data.get('rows', []))} rows → {out_path}")
