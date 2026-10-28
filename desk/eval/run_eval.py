"""eval/run_eval.py — per-component pass/fail vs the golden set.

Sections:
  1. fees    — fee_for matches hand-computed values EXACTLY (fees are never tuned).
  2. engine  — ladder-walk fills match analytic avg price / fee on synthetic books.
  3. sample  — the shipped example strategy runs end-to-end over a synthetic store:
               orders emitted, taker fill walks the ladder, fee matches golden, maker rests.
  4. recon   — golden_replay deviation vs real fills; GATES once enough overlap exists.

Exit nonzero if any HARD section fails. `make eval` runs this; `--fast` runs the
store/network-free HARD guards only (seconds), for a pre-merge gate.
"""
from __future__ import annotations

import json
import os
import sys
from typing import List

from desk.eval.api import Section
from desk.types import Order, BookSnapshot
from desk.sim import fees as fees_mod
from desk.sim.engine import execute
from desk.sim.fill_source import SimFillSource
from desk.data.store import DataAtT

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "engine_fees.json")
FEE_TOL = 1e-4
PRICE_TOL = 1e-6


# ── in-memory store for synthetic engine cases (no filesystem) ────────────────
class InMemoryStore:
    def __init__(self, book: BookSnapshot):
        self.book = book

    def route(self, market_id, token_id, t) -> DataAtT:
        return DataAtT(market_id, token_id, t, tier="T5", book=self.book,
                       mid=self.book.mid, as_of_ts=t, stale_secs=0.0, degraded=False)

    def prints_between(self, *a, **k):
        return []


def _book_from(spec: dict) -> BookSnapshot:
    return BookSnapshot(
        market_id="M", token_id="TK", capture_ts=0, exchange_ts=0, hash="h",
        bids=spec.get("bids", []), asks=spec.get("asks", []),
        best_bid=(spec.get("bids") or [[0, 0]])[0][0],
        best_ask=(spec.get("asks") or [[0, 0]])[0][0],
        mid=spec.get("mid", 0.0), spread=0.0, tick_size=0.001,
        min_order_size=1, neg_risk=False, last_trade_price=None)


def check_fees(g) -> Section:
    s = Section("fees")
    # PIN the taker-fees knob ON: this golden verifies the fee FORMULA, not the deployment choice.
    # Otherwise a shell that exported DESK_TAKER_FEES_ENABLED=false would see fee_for() short-circuit
    # to 0 and fail every nonzero-fee case with no real defect.
    _prev = os.environ.get("DESK_TAKER_FEES_ENABLED")
    os.environ["DESK_TAKER_FEES_ENABLED"] = "true"
    try:
        for c in g["fees"]:
            got = fees_mod.fee_for(c["shares"], c["price"], mode=c.get("mode", "taker"),
                                   category=c.get("category"), ts=c.get("ts"))
            ok = abs(got - c["expected_fee"]) < FEE_TOL
            s.check(ok, c["name"], f"expected {c['expected_fee']} got {got}")
    finally:
        if _prev is None:
            os.environ.pop("DESK_TAKER_FEES_ENABLED", None)
        else:
            os.environ["DESK_TAKER_FEES_ENABLED"] = _prev
    return s


def check_engine(g) -> Section:
    s = Section("engine")
    for c in g["engine"]:
        store = InMemoryStore(_book_from(c["book"]))
        o = c["order"]
        order = Order(order_id="g", strategy_id="g", market_id="M", token_id="TK",
                      side=o["side"], submit_ts=c.get("ts", 0), limit_price=o["limit_price"],
                      qty=o["qty"], mode=o.get("mode", "taker"))
        src = SimFillSource(store, category_of=lambda _m, _c=c: _c.get("category"))
        rep = execute(order, src)
        exp = c["expected"]
        s.check(abs(rep.filled_qty - exp["filled_qty"]) < PRICE_TOL,
                c["name"] + " :: filled_qty", f"exp {exp['filled_qty']} got {rep.filled_qty}")
        s.check(abs(rep.avg_fill_price - exp["avg_fill_price"]) < PRICE_TOL,
                c["name"] + " :: avg_price", f"exp {exp['avg_fill_price']} got {rep.avg_fill_price}")
        if "fees" in exp:
            s.check(abs(rep.fees - exp["fees"]) < 1e-3,
                    c["name"] + " :: fees", f"exp {exp['fees']} got {rep.fees}")
        if "degraded" in exp:
            s.check(rep.degraded == exp["degraded"],
                    c["name"] + " :: degraded", f"exp {exp['degraded']} got {rep.degraded}")
    return s


def check_run_dir() -> Section:
    """HARD smoke: the backtest lane must emit desk/eval/runs/<id>/{run.json,trades.jsonl}
    and the ONE dashboard adapter must load it. Synthetic store; no capture data needed."""
    s = Section("run_dir", hard=True)
    import shutil
    import subprocess
    run_id = "eval-smoke-run"
    run_dir = os.path.join("desk", "eval", "runs", run_id)
    try:
        from desk.backtest.runner import run, RunConfig
        store = InMemoryStore(_book_from({"bids": [[0.45, 100]], "asks": [[0.55, 100]], "mid": 0.5}))
        o = Order(order_id="rs", strategy_id="evalsmoke", market_id="M", token_id="TK", side="BUY",
                  submit_ts=1781568000, limit_price=0.99, qty=2, mode="taker")
        run([o], RunConfig(store=store, run_id=run_id, source="backtest", strategy="evalsmoke",
                           config={"smoke": True}, mark_mode="market"))
        s.check(os.path.exists(os.path.join(run_dir, "run.json")), "run.json emitted")
        rows = 0
        tpath = os.path.join(run_dir, "trades.jsonl")
        if os.path.exists(tpath):
            rows = sum(1 for _ in open(tpath))
        s.check(rows >= 1, "trades.jsonl has >=1 TradeRow", f"rows={rows}")
        adapter = os.path.join(".claude", "skills", "desk-dashboard", "scripts", "adapters", "ledger.py")
        if os.path.exists(adapter):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tf:
                page_out = tf.name                  # --out = a RENDERED dashboard page (html), not raw spec
            out = subprocess.run([sys.executable, adapter, "--source", "backtest", "--run", run_id,
                                  "--out", page_out], capture_output=True, text=True, timeout=60)
            ok = out.returncode == 0
            if ok:
                try:
                    ok = "evalsmoke" in open(page_out).read()   # the smoke strategy rendered into the page
                except Exception:
                    ok = False
            os.unlink(page_out)
            s.check(ok, "ledger adapter loads+renders the run", (out.stderr or out.stdout or "")[-200:])
        else:
            s.notes.append("dashboard adapter absent — adapter-load check skipped")
    except Exception as e:
        s.check(False, "run-dir smoke ran", repr(e))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return s


class _TwoBookStore:
    """Synthetic in-memory store with two book snapshots for ONE token, so the sample
    strategy's generate_orders has a universe to scan and the engine has a real ladder
    to walk. No filesystem, no network."""
    def __init__(self):
        self._token = ("M", "TK")
        t0, t1 = 1781568000, 1781568300
        self._books = {
            t0: _book_from({"bids": [[0.44, 200]], "asks": [[0.46, 200]], "mid": 0.45}),
            t1: _book_from({"bids": [[0.55, 200]], "asks": [[0.57, 200]], "mid": 0.56}),
        }
        self._ts = sorted(self._books)

    def universe(self):
        m, tk = self._token
        return [(m, tk, self._ts[0], self._ts[-1], len(self._ts))]

    def _nearest(self, t):
        prior = [x for x in self._ts if x <= t]
        return self._books[prior[-1] if prior else self._ts[0]]

    def route(self, market_id, token_id, t) -> DataAtT:
        b = self._nearest(t)
        return DataAtT(market_id, token_id, t, tier="T5", book=b, mid=b.mid,
                       as_of_ts=t, stale_secs=0.0, degraded=False)

    def book_at(self, market_id, token_id, t):
        return self._nearest(t)

    def prints_between(self, *a, **k):
        return []

    def resolved_markets(self):
        return []


def check_sample() -> Section:
    """HARD: the shipped example strategy priced+executed on a synthetic 2-snapshot store.
    Asserts orders are emitted, a taker order walks the real ladder at the expected avg
    price, its fee matches the fee schedule for its category, and a maker order rests."""
    s = Section("sample", hard=True)
    try:
        from desk.signals.example_calendar import generate_orders
        store = _TwoBookStore()
        orders = generate_orders(store)
        s.check(bool(orders), "example strategy emits orders on the synthetic store",
                f"n_orders={len(orders)}")
        if not orders:
            return s

        # taker fill: a deep marketable BUY walks the ask ladder → avg = the ask price.
        book = store.book_at("M", "TK", 1781568000)
        ask_px = book.asks[0][0]
        taker = Order(order_id="smk-taker", strategy_id="sample", market_id="M", token_id="TK",
                      side="BUY", submit_ts=1781568000, limit_price=0.99, qty=5, mode="taker")
        cat = "politics"
        rep = execute(taker, SimFillSource(store, category_of=lambda _m: cat))
        s.check(rep.filled_qty > 0, "taker fills", f"filled_qty={rep.filled_qty}")
        s.check(abs(rep.avg_fill_price - ask_px) < 1e-6,
                "taker avg price = the ask (walked the real ladder)",
                f"avg={rep.avg_fill_price} ask={ask_px}")
        exp_fee = fees_mod.fee_for(rep.filled_qty, rep.avg_fill_price, mode="taker",
                                   category=cat, ts=1781568000)
        s.check(abs(rep.fees - exp_fee) < 1e-3, "taker fee matches the fee schedule",
                f"got {rep.fees} exp {exp_fee}")

        # maker order rests: a passive BUY below the touch does not cross → unfilled, tracked.
        maker = Order(order_id="smk-maker", strategy_id="sample", market_id="M", token_id="TK",
                      side="BUY", submit_ts=1781568000, limit_price=0.30, qty=5, mode="maker")
        mrep = execute(maker, SimFillSource(store, category_of=lambda _m: cat))
        s.check(mrep.filled_qty <= 0 and mrep.unfilled_qty > 0,
                "maker order rests (does not cross the book)",
                f"filled={mrep.filled_qty} unfilled={mrep.unfilled_qty}")
    except Exception as e:
        s.check(False, "sample section ran", repr(e))
    return s


def check_backtest() -> Section:
    s = Section("backtest", hard=False)
    try:
        from desk.backtest.runner import run, RunConfig
        from desk.signals.example_calendar import generate_orders
        from desk.data.store import Store
        store = Store()
        orders = generate_orders(store)
        if not orders:
            s.notes.append("no captured data -> backtest smoke skipped")
            return s
        res = run(orders, RunConfig(store=store, starting_cash=1000.0))
        s.check(len(res.equity_curve) > 0, "equity_curve non-empty")
        s.check("by_tier" in res.data_quality, "data_quality present")
        s.check(0.0 <= res.fill_ratio <= 1.0, "fill_ratio in [0,1]", str(res.fill_ratio))
        s.notes.append(f"n_orders={len(orders)} total_pnl={res.total_pnl} "
                       f"fill_ratio={res.fill_ratio} dq={res.data_quality}")
    except Exception as e:
        s.check(False, "backtest ran", repr(e))
    return s


# Bookless T1 store: route() returns tier T1, book=None, mid=.5, mimicking a mid-only tier with NO ladder.
# It deliberately lacks `_load_market_books` and `sources`, so fill_model._recent_vol yields None (no series
# to measure a range from) — the exact precondition for the T1 cost fail-closed skip.
class _T1BooklessStore:
    def route(self, market_id, token_id, t) -> DataAtT:
        return DataAtT(market_id, token_id, t, tier="T1", book=None,
                       mid=0.5, as_of_ts=t, stale_secs=0.0, degraded=True)

    def prints_between(self, *a, **k):
        return []


def check_wide_book() -> Section:
    """HARD regression: on a synthetic WIDE book, net execution must be much worse than
    gross-at-mid — the engine charges the real spread, never fills at mid. Also asserts the
    base maker queue share stays < 1.0, AND the T1 cost fail-closed: a T1 (bookless, mid-only)
    fill with a cost model engaged but NO vol/regime signal (Order.entry_vol=None AND
    _recent_vol=None) must SKIP — never silently price on the cheap calm tier — emitting the
    'cost fail-closed' warning."""
    s = Section("wide_book", hard=True)
    try:
        from desk.sim.fill_model import Assumptions
        # wide thin book: bid .40 / ask .60 (mid .50). A taker BUY must pay ~the ask, ≥9c/sh worse than mid.
        book = _book_from({"bids": [[0.40, 50]], "asks": [[0.60, 50]], "mid": 0.50})
        store = InMemoryStore(book)
        order = Order(order_id="wb", strategy_id="wb", market_id="M", token_id="TK",
                      side="BUY", submit_ts=1781568000, limit_price=0.999, qty=10, mode="taker")
        rep = execute(order, SimFillSource(store, category_of=lambda _m: "politics"))
        cost_vs_mid = rep.avg_fill_price - 0.50
        s.check(rep.filled_qty > 0, "wide-book taker fills")
        s.check(cost_vs_mid >= 0.09, "net << gross: BUY pays ~the ask, not mid",
                f"avg_fill={rep.avg_fill_price} (cost vs mid {cost_vs_mid:.3f})")
        s.check(rep.fees > 0, "taker fee charged on the fill", str(rep.fees))
        s.check(Assumptions().queue_fill_prob < 1.0,
                "base Assumptions.queue_fill_prob < 1.0 (opt bracket only)",
                str(Assumptions().queue_fill_prob))
        # T1 cost fail-closed: a dummy (any) cost_model object engages the fail-closed guard; with
        # entry_vol=None and _recent_vol=None the market is SKIPPED (never priced on the calm tier).
        t1_order = Order(order_id="t1", strategy_id="t1", market_id="M", token_id="TK",
                         side="BUY", submit_ts=1781568000, limit_price=0.999, qty=10, mode="taker")
        t1_rep = execute(t1_order, SimFillSource(_T1BooklessStore(), category_of=lambda _m: "politics",
                                                 cost_model=object()))
        s.check(t1_rep.filled_qty == 0, "T1 no-vol cost fail-closed: NO fill (not priced on calm tier)",
                f"filled_qty={t1_rep.filled_qty}")
        s.check(any("cost fail-closed" in w for w in t1_rep.warnings),
                "T1 no-vol cost fail-closed: warning emitted", f"warnings={t1_rep.warnings}")
    except Exception as e:
        s.check(False, "wide-book regression ran", repr(e))
    return s


# This checkout's root — the tree the supervisor units must launch code from. The invariant is simply
# "supervisor units run committed code from THIS checkout": the entrypoint each unit starts is a
# git-tracked `-m desk.*` module living here. Path-independent, so it holds on any machine/OS.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git_tracked(repo: str, path: str) -> bool:
    """True iff `path` is TRACKED by git (in the index/HEAD), via `git ls-files --error-unmatch` from the repo
    root. Stronger than os.path.exists: an UNtracked file that merely exists on disk (a stray plist in scripts/,
    an entrypoint that was never committed) is NOT tracked, so it FAILS the gate. `path` may be absolute or
    repo-relative; git resolves it against cwd=repo."""
    import subprocess
    rel = os.path.relpath(path, repo) if os.path.isabs(path) else path
    try:
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _entrypoint_module(cmd: str):
    """(dotted, abs_path) of the `-m desk.*` entrypoint in a unit command, or (None, None)."""
    import re
    m = re.search(r"-m (desk\.[a-zA-Z_.]+)", cmd)
    if not m:
        return None, None
    return m.group(1), os.path.join(REPO_ROOT, m.group(1).replace(".", os.sep) + ".py")


def check_supervisor() -> Section:
    """HARD guard: every process-supervisor unit shipped in this repo launches COMMITTED code from THIS
    checkout — a git-tracked `-m desk.*` entrypoint — with a documented EnvironmentFile and no behavior-flag
    or secret leakage baked into the unit. Kills the stale-code-daemon + env-contamination classes. Two unit
    families, each checked only when its files are present, so a clean tree stays green either way:

      • scripts/systemd/*.service  — the server (Linux) supervisor; path-independent (%h placeholder).
      • scripts/com.desk.*.plist   — optional macOS convenience; carries an absolute path, checked only for
                                      self-consistency (cd target == WorkingDirectory, logs under it). Skipped
                                      entirely when DESK_SUPERVISOR=systemd or when no plists are present.

    git-tracked assertions run only once THIS repo is its own git root (`.git` at the root); before that
    (fresh unpack, or nested under another tree) the content checks still run."""
    s = Section("supervisor", hard=True)
    try:
        import glob as _glob
        repo = REPO_ROOT
        own_git_root = os.path.exists(os.path.join(repo, ".git"))
        if not own_git_root:
            s.notes.append("repo is not its own git root yet — git-tracked checks skipped "
                           "(run after `git init` in this repo); content checks still run")

        # ── systemd units (server-first) ─────────────────────────────────────
        services = sorted(_glob.glob(os.path.join(repo, "scripts", "systemd", "*.service")))
        for p in services:
            name = os.path.basename(p)
            body = open(p).read()
            # directive lines only — a `#` comment may legitimately name a flag while teaching the operator.
            directives = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
            if own_git_root:
                s.check(_git_tracked(repo, p), f"{name} :: unit is git-tracked")
            execs = [ln.split("=", 1)[1].strip() for ln in directives.splitlines()
                     if ln.strip().startswith("ExecStart=")]
            s.check(len(execs) == 1, f"{name} :: exactly one ExecStart", f"n={len(execs)}")
            cmd = execs[0] if execs else ""
            dotted, mod = _entrypoint_module(cmd)
            s.check(dotted is not None, f"{name} :: ExecStart runs a -m desk.* entrypoint", cmd)
            if mod:
                s.check(os.path.exists(mod), f"{name} :: entrypoint module exists in this checkout", mod)
                if own_git_root:
                    s.check(_git_tracked(repo, mod), f"{name} :: entrypoint module git-tracked", mod)
            s.check("EnvironmentFile=" in directives, f"{name} :: EnvironmentFile documented")
            s.check("LIVE_TRADING_ENABLED" not in directives, f"{name} :: no behavior flags baked into the unit")
            s.check("POLY_PRIVATE_KEY" not in directives and "POLY_API_SECRET" not in directives,
                    f"{name} :: no secrets baked into the unit")

        # ── macOS launchd plists (optional convenience) ──────────────────────
        plists = [p for p in _glob.glob(os.path.join(repo, "scripts", "com.desk.*.plist"))
                  if not p.endswith("TEMPLATE.plist")]
        if os.environ.get("DESK_SUPERVISOR", "").lower() == "systemd":
            s.notes.append("DESK_SUPERVISOR=systemd — plist checks skipped (server deploy)")
        elif not plists:
            s.notes.append("no plists present — plist checks skipped (systemd/other supervisor)")
        else:
            import plistlib
            for p in plists:
                name = os.path.basename(p)
                if own_git_root:
                    s.check(_git_tracked(repo, p), f"{name} :: plist is git-tracked (not a stray untracked plist)")
                with open(p, "rb") as f:
                    d = plistlib.load(f)
                cmd = " ".join(d.get("ProgramArguments", []))
                wd = d.get("WorkingDirectory", "")
                # self-consistency: the cd target, WorkingDirectory and log paths all name ONE directory and
                # the command runs a tracked entrypoint from it (no dev-checkout / stale-code drift).
                s.check(bool(wd) and f'cd "{wd}"' in cmd, f"{name} :: command cds into WorkingDirectory", wd)
                s.check("source" not in cmd or ".env" not in cmd, f"{name} :: no wholesale .env sourcing")
                envs = set((d.get("EnvironmentVariables") or {}).keys())
                s.check(envs <= {"OMP_NUM_THREADS"}, f"{name} :: pinned-minimal env",
                        f"extra={envs - {'OMP_NUM_THREADS'}}")
                s.check("LIVE_TRADING_ENABLED" not in cmd, f"{name} :: no behavior flags in command")
                dotted, mod = _entrypoint_module(cmd)
                s.check(dotted is not None, f"{name} :: python -m desk.* entrypoint")
                if mod:
                    s.check(os.path.exists(mod), f"{name} :: entrypoint module exists", mod)
                    if own_git_root:
                        s.check(_git_tracked(repo, mod), f"{name} :: entrypoint tracked in repo", mod)
                for k in ("StandardOutPath", "StandardErrorPath"):
                    s.check(str(d.get(k, "")).startswith(wd), f"{name} :: {k} under WorkingDirectory")

        # the repo ships at least one supervisor unit family (server units are the product)
        s.check(bool(services) or bool(plists), "at least one supervisor unit family present",
                f"systemd={len(services)} plists={len(plists)}")
    except Exception as e:
        s.check(False, "supervisor guard ran", repr(e))
    return s


RECON_MIN_HC = 20            # need >= this many high-confidence sim-vs-real overlaps before the gate can FAIL
RECON_P90_TOL_BPS = 200.0    # backtest fill price must track real fills within this p90 (generous; realized~half-tick)
RECON_FEE_DEV_TOL = 0.01     # base fee_dev (sim formula vs real fee) must be ~exact ($/fill)


def check_recon() -> Section:
    """GATING once real overlap exists — the one invariant that catches backtest!=live execution drift.
    golden_replay replays our decisions over REAL fills (captured prints) and reports high_conf price dev
    (sim_avg vs real) + base fee_dev. When >= RECON_MIN_HC high-confidence overlaps exist, the backtest
    MUST reproduce the live fill price (p90) and the fee formula (exact) or the gate FAILS. Below that
    count the overlap is too thin to gate — reported, never fails (a clean tree / sparse-fills day stays
    green). The fee-RATE assumption is a separate lever owned by fees.taker_fees_enabled(); golden_replay
    feeds the real per-market rate so fee_dev tests the FORMULA, not the rate."""
    s = Section("recon", hard=True)
    try:
        from desk.eval.recon.golden_replay import run as replay
        rep = replay(max_tokens=60)   # scan enough tokens to accumulate overlap once real fills exist
        s.notes.append(f"golden_replay: {rep}")
        n = rep.get("high_conf_n") or 0
        if n >= RECON_MIN_HC:
            p90 = rep.get("high_conf_p90_bps")
            s.check(p90 is not None and p90 <= RECON_P90_TOL_BPS,
                    f"backtest reproduces live fill price (high_conf p90 <= {RECON_P90_TOL_BPS}bps)",
                    f"n={n} p90={p90}")
            fdev = rep.get("high_conf_max_fee_dev_base")
            if fdev is not None:
                s.check(fdev <= RECON_FEE_DEV_TOL,
                        f"base fee_dev ~exact (max <= {RECON_FEE_DEV_TOL})", f"fee_dev={fdev}")
        else:
            s.notes.append(f"recon non-gating: only {n} high-confidence overlaps (< {RECON_MIN_HC}) — "
                           f"too thin to gate; will gate once real fills accumulate")
    except Exception as e:
        # INTENTIONAL non-fail (unlike other hard sections): golden_replay needs captured prints +
        # market data that are legitimately absent on a clean tree — unavailability is not a code
        # regression. The hard gate bites only on MEASURED drift above.
        s.notes.append(f"golden_replay skipped (non-gating — no fill data): {e!r}")
    return s


def check_registry() -> Section:
    """HARD guard: pre-registration integrity. Every strategy_config row whose base is available
    re-seals to the declared value (seal = EFFECTIVE merged behavior + base version); a sealed+enabled
    row loads when its base is available and is REFUSED (not silently swapped) when it isn't; a
    tampered row is REFUSED by load_registry (fail-closed). Desk-only safe: rows naming an absent
    tenant are unverifiable by design — the guard asserts they do NOT load."""
    s = Section("registry", hard=True)
    try:
        from desk.signals import registry as R
        cfg = json.load(open(R.config_path()))   # honors DESK_STRATEGY_CONFIG
        rows = cfg.get("strategies", [])
        sealed = [r for r in rows if r.get("config_hash")]
        s.check(len(sealed) >= 1, "at least one hash-sealed (pre-registered) row exists")
        bases = R.base_specs()
        verifiable = []
        for r in sealed:
            expected = R.seal_hash_for(r, bases)
            if expected is None:
                s.notes.append(f"row '{r['name']}': base '{r.get('strategy', r['name'])}' not "
                               f"discovered — seal unverifiable here (loader refuses it loudly)")
                continue
            verifiable.append(r)
            s.check(r["config_hash"] == expected, f"seal verifies (effective behavior) :: {r['name']}",
                    f"declared {r['config_hash'][:12]}… expected {expected[:12]}… — if the base's "
                    f"defaults/version changed on purpose, reseal (registry CLI)")
        names = {sp.name for sp in R.load_registry()}
        for r in sealed:
            if not r.get("enabled"):
                continue
            if any(v is r for v in verifiable):
                s.check(r["name"] in names, f"sealed+enabled row loads :: {r['name']}")
            else:
                s.check(r["name"] not in names,
                        f"sealed+enabled row with ABSENT base is refused, not base-swapped :: {r['name']}")
        # tamper test: a mutated copy of a sealed row must be refused
        if sealed:
            import copy
            bad = copy.deepcopy(cfg)
            for r in bad["strategies"]:
                if r.get("config_hash"):
                    r.setdefault("params", {})["entry_threshold"] = 0.999
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(bad, f)
                tmp = f.name
            tampered = {sp.name for sp in R.load_registry(tmp)}
            os.unlink(tmp)
            s.check(all(r["name"] not in tampered for r in sealed if r.get("config_hash")),
                    "tampered sealed row is refused (fail-closed)")
    except Exception as e:
        s.check(False, "registry guard ran", repr(e))
    return s


def check_discovery_hook() -> Section:
    """HARD guard (pre-flip-generality P1): date-discovery slug enumeration. Tenant
    `discover_event_slugs(window_days)` hooks pass through verbatim; the DEPRECATED
    `date_discovery_cities` alias (tenant hook or lane key) wraps city lists through the legacy
    temperature template byte-identically; contributions compose + dedupe; a raising hook is
    skipped without aborting the others; the empty case is empty (and loud), never a crash.
    Store/network-free — hooks are injected, nothing touches the registry or Gamma."""
    s = Section("discovery_hook", hard=True)
    try:
        import datetime
        from desk.data.capture_select import _discovery_slugs
        today = datetime.date(2026, 7, 9)
        lane = {"date_discovery_days": 1}

        # 1. new hook: verbatim passthrough + window plumbed through
        seen_days = {}
        def _new(days):
            seen_days["days"] = days
            return ["game-day-slug-a", "game-day-slug-b"]
        got = _discovery_slugs(lane, hooks=({"sports": _new}, {}), today=today)
        s.check(got == ["game-day-slug-a", "game-day-slug-b"],
                "new-hook slugs pass through verbatim (no framework grammar)", repr(got))
        s.check(seen_days.get("days") == 1, "hook receives the lane window (days)", repr(seen_days))

        # 2. deprecated alias == the exact legacy template enumeration (day → metric → city)
        exp = []
        for d in range(2):
            day = today + datetime.timedelta(days=d)
            mon, dom, yr = day.strftime("%B").lower(), day.day, day.year
            for metric in ("highest", "lowest"):
                for city in ("london", "nyc"):
                    exp.append(f"{metric}-temperature-in-{city}-on-{mon}-{dom}-{yr}")
        got = _discovery_slugs(lane, hooks=({}, {"wx": lambda: ["nyc", "london"]}), today=today)
        s.check(got == exp, "alias wraps cities through the EXACT legacy template (sorted union)",
                f"got[0]={got[0] if got else None} exp[0]={exp[0]}")

        # 3. compose + dedupe; a tenant declaring BOTH hooks contributes via the NEW one only
        got = _discovery_slugs(lane, hooks=({"a": lambda d: ["s1", "s1", "s2"]},
                                            {"a": lambda: ["ignored-city"],
                                             "b": lambda: ["london"]}), today=today)
        s.check(got[:2] == ["s1", "s2"] and got.count("s1") == 1,
                "new-hook slugs dedupe, contribution order kept", repr(got[:3]))
        s.check(not any("ignored-city" in x for x in got),
                "tenant with BOTH hooks uses the new one only", repr(got))
        s.check(any(x == "highest-temperature-in-london-on-july-9-2026" for x in got),
                "other tenant's alias cities still wrapped (composition)", repr(got[-2:]))

        # 4. lane-config city list keeps legacy precedence over tenant old-hooks
        lane2 = {"date_discovery_days": 0, "date_discovery_cities": ["paris"],
                 "date_discovery_metrics": ["highest"]}
        got = _discovery_slugs(lane2, hooks=({}, {"wx": lambda: ["london"]}), today=today)
        s.check(got == ["highest-temperature-in-paris-on-july-9-2026"],
                "lane city list overrides tenant old-hook cities (legacy precedence)", repr(got))

        # 5. a raising hook is loudly skipped; others still contribute
        def _boom(days):
            raise RuntimeError("boom")
        got = _discovery_slugs(lane, hooks=({"bad": _boom, "good": lambda d: ["ok-slug"]}, {}),
                               today=today)
        s.check(got == ["ok-slug"], "raising hook skipped, others contribute", repr(got))

        # 6. nothing declared anywhere -> empty, not a crash (the loud print is the operator signal)
        got = _discovery_slugs({"date_discovery_days": 0}, hooks=({}, {}), today=today)
        s.check(got == [], "no hooks + no lane list -> empty (loud, never silent discovery)", repr(got))
    except Exception as e:
        s.check(False, "discovery_hook guard ran", repr(e))
    return s


def check_recon_lifecycle() -> Section:
    """HARD guard: live-fill reconciliation + maker cancel/expiry — the plumbing that must exist before any
    maker-scope PASS. All offline (mocked CLOB client / fake adapter; NO creds, NO network):
      • adapter._fills_from_trades books BOTH sides — we-were-taker AND we-rested-as-maker.
      • adapter.cancel gates on LIVE_TRADING_ENABLED and, when enabled, calls cancel_order(OrderPayload(orderID)).
      • reconcile_live_orders no-ops when not live; expires+cancels a maker past its expiry_ts and drops the row;
        books a late fill on a not-yet-expired maker into the position + a TradeRow.
    """
    s = Section("recon_lifecycle", hard=True)
    try:
        import os as _os
        from desk.live.clob_adapter import ClobAdapter
        from desk.types import Order

        # ── parser: taker + maker sides ──────────────────────────────────────
        ad = ClobAdapter()
        ad.fee_divisor = 1.0            # trust fees 1:1 so the assertions are exact
        trades = [
            {"taker_order_id": "OURTAKER", "asset_id": "TK", "price": 0.40, "size": 3.0, "fee": 0.01, "match_time": 100},
            {"taker_order_id": "SOMEONE", "asset_id": "TK", "price": 0.41, "size": 9.0, "fee": 0.0, "match_time": 101,
             "maker_orders": [{"order_id": "OURMAKER", "matched_amount": 2.0, "price": 0.39, "fee": 0.002},
                              {"order_id": "OTHER", "matched_amount": 7.0, "price": 0.39, "fee": 0.0}]},
            {"taker_order_id": "NOPE", "asset_id": "TK", "price": 0.5, "size": 5.0, "fee": 0.0, "match_time": 102},
        ]
        taker = ad._fills_from_trades(trades, "OURTAKER", [])
        s.check(len(taker) == 1 and abs(taker[0].qty - 3.0) < 1e-9 and abs(taker[0].price - 0.40) < 1e-9,
                "parser books our TAKER fill", str([(f.qty, f.price) for f in taker]))
        maker = ad._fills_from_trades(trades, "OURMAKER", [])
        s.check(len(maker) == 1 and abs(maker[0].qty - 2.0) < 1e-9 and abs(maker[0].price - 0.39) < 1e-9,
                "parser books our MAKER leg only (not the co-maker)", str([(f.qty, f.price) for f in maker]))
        s.check(ad._fills_from_trades(trades, "ABSENT", []) == [], "parser: unknown order → no fills")

        # ── cancel: gated + correct client call ──────────────────────────────
        class _MockClient:
            def __init__(self): self.cancelled = []
            def cancel_order(self, payload): self.cancelled.append(payload.orderID); return {"canceled": [payload.orderID]}
            def get_order(self, oid): return {"status": "LIVE"}
            def get_trades(self, params=None, **k): return []
        prev = _os.environ.get("LIVE_TRADING_ENABLED")
        try:
            _os.environ["LIVE_TRADING_ENABLED"] = "false"
            ad2 = ClobAdapter()
            refused = False
            try:
                ad2.cancel("X")
            except RuntimeError:
                refused = True
            s.check(refused, "cancel REFUSES when LIVE_TRADING_ENABLED=false (real signed write)")
            _os.environ["LIVE_TRADING_ENABLED"] = "true"
            ad3 = ClobAdapter()
            mc = _MockClient()
            ad3._client = mc                                  # inject: no _connect / no creds / no geo network
            # geo_gate must pass for the cancel to reach the client; patch it to OK for the offline test
            import desk.live.clob_adapter as CA
            _real_geo = CA.geo_gate
            CA.geo_gate = lambda: type("G", (), {"ok": True, "place": "", "reason": "", "country": ""})()
            try:
                ad3.cancel("ORD123")
            finally:
                CA.geo_gate = _real_geo
            s.check(mc.cancelled == ["ORD123"], "cancel → client.cancel_order(OrderPayload(orderID))", str(mc.cancelled))
        finally:
            if prev is None:
                _os.environ.pop("LIVE_TRADING_ENABLED", None)
            else:
                _os.environ["LIVE_TRADING_ENABLED"] = prev

        # ── reconcile pass: no-op off-live; expiry sweep cancels+drops a maker ─
        from desk.driver import strategy_driver as SD
        s.check(SD.reconcile_live_orders(None, None, [])["polled"] == 0,
                "reconcile_live_orders no-ops when not live (no creds/exchange)")

        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            _real_dir = SD.DRIVER_DIR
            SD.DRIVER_DIR = td
            _real_live = SD._live_enabled
            expired_ts = 1000                                # long past
            row = {"exchange_order_id": "MK1", "strategy_id": "t", "market_id": "M", "token_id": "TK",
                   "side": "BUY", "mode": "maker", "submit_ts": 500, "expiry_ts": expired_ts,
                   "limit_price": 0.3, "qty": 5.0, "booked_qty": 0.0, "config_hash": "h", "slug": "s"}
            _json.dump([row], open(_os.path.join(td, "live_orders_t.json"), "w"))

            class _FakeOF:
                def __init__(self): self.fills, self.status, self.degraded, self.warnings = [], "LIVE", False, []
                filled_qty = 0.0
            class _FakeAdapter:
                def __init__(self): self.cancelled = []
                def poll_fills_for_order(self, xid, token_id="", known_qty=0.0): return _FakeOF()
                def cancel(self, xid): self.cancelled.append(xid)
            fake = _FakeAdapter()
            import desk.live.clob_adapter as CA2
            _real_cls = CA2.ClobAdapter
            CA2.ClobAdapter = lambda: fake
            SD._live_enabled = lambda: True
            _prev2 = _os.environ.get("LIVE_TRADING_ENABLED")
            _os.environ["LIVE_TRADING_ENABLED"] = "true"
            try:
                out = SD.reconcile_live_orders(None, None, ["t"])
                remaining = _json.load(open(_os.path.join(td, "live_orders_t.json")))
                s.check(out["expired_cancelled"] == 1 and fake.cancelled == ["MK1"],
                        "expiry sweep cancels the past-expiry maker", f"out={out} cancelled={fake.cancelled}")
                s.check(remaining == [], "expired maker row dropped from tracking", str(remaining))

                # a late fill on a NOT-yet-expired maker is booked into the position + a TradeRow
                from desk.types import Fill as _Fill
                from desk.tracker.pnl_ledger import PnLLedger
                future_ts = int(__import__("time").time()) + 3600
                row2 = {"exchange_order_id": "MK2", "strategy_id": "t", "market_id": "M2", "token_id": "TK2",
                        "side": "BUY", "mode": "maker", "submit_ts": 500, "expiry_ts": future_ts,
                        "limit_price": 0.4, "qty": 5.0, "booked_qty": 0.0, "config_hash": "h", "slug": "s2"}
                _json.dump([row2], open(_os.path.join(td, "live_orders_t.json"), "w"))

                class _OF2:
                    def __init__(self): self.fills = [_Fill(ts=future_ts - 10, price=0.40, qty=3.0, fee=0.02)]
                    status = "LIVE"; degraded = False; warnings = []
                    @property
                    def filled_qty(self): return 3.0
                class _FakeAdapter2:
                    def poll_fills_for_order(self, xid, token_id="", known_qty=0.0): return _OF2()
                    def cancel(self, xid): pass
                CA2.ClobAdapter = lambda: _FakeAdapter2()
                trades_path = _os.path.join(td, "trades.jsonl")
                ledger = PnLLedger(path=_os.path.join(td, "ledger.json"), trades_path=trades_path)
                out2 = SD.reconcile_live_orders(None, ledger, ["t"])
                pos = SD._load_positions("t")
                booked_pos = pos.get(("M2", "TK2"), {}).get("qty", 0.0)
                s.check(out2["reconciled_fills"] == 1 and abs(booked_pos - 3.0) < 1e-9,
                        "books a late fill into the position", f"out={out2} pos_qty={booked_pos}")
                nrows = sum(1 for _ in open(trades_path)) if _os.path.exists(trades_path) else 0
                s.check(nrows >= 1, "emits a TradeRow for the reconciled fill", f"rows={nrows}")
                # a reconciled fill is a REAL exchange fill (tier LIVE, not the T5 sim ladder-walk) and its
                # send-time mid is unknowable, so slippage is None — never a fake 0.0.
                recon_rows = [_json.loads(l) for l in open(trades_path)] if _os.path.exists(trades_path) else []
                mk2 = [r for r in recon_rows if r.get("token_id") == "TK2"]
                s.check(bool(mk2) and all(r.get("fidelity_tier") == "LIVE" for r in mk2),
                        "reconciled fill TradeRow carries fidelity_tier=LIVE (not T5)",
                        str([r.get("fidelity_tier") for r in mk2]))
                s.check(bool(mk2) and all((r.get("extras") or {}).get("slippage_bps_vs_mid") is None for r in mk2),
                        "reconciled fill slippage_bps_vs_mid is None (send-time mid unknowable)",
                        str([(r.get("extras") or {}).get("slippage_bps_vs_mid") for r in mk2]))
                left = _json.load(open(_os.path.join(td, "live_orders_t.json")))
                s.check(left and abs((left[0].get("booked_qty") or 0) - 3.0) < 1e-9,
                        "advances booked_qty (partial fill stays tracked)", str(left))
            finally:
                CA2.ClobAdapter = _real_cls
                SD.DRIVER_DIR = _real_dir
                SD._live_enabled = _real_live
                if _prev2 is None:
                    _os.environ.pop("LIVE_TRADING_ENABLED", None)
                else:
                    _os.environ["LIVE_TRADING_ENABLED"] = _prev2
    except Exception as e:
        s.check(False, "recon_lifecycle guard ran", repr(e))
    return s


def check_compaction_recount() -> Section:
    """HARD guard: store.universe()'s n_snaps must equal the true row count AFTER hour-*.parquet compaction.
    A naive accumulator that re-counts a compacted hour's rows (parts counted, then the merged hour file
    counted again on top — deleted parts never decrement) skews every richest-first n_snaps consumer
    (golden_replay / slippage_calibration). Fully synthetic: write part-*.parquet, index, compact(), re-index,
    assert n_snaps == actual parquet rows. No live store."""
    s = Section("compaction_recount", hard=True)
    try:
        import glob as _glob
        import tempfile
        import time as _t
        import pyarrow as _pa
        import pyarrow.parquet as _pq
        from desk.data.store import Store
        from desk.data.capture_validate import compact

        with tempfile.TemporaryDirectory() as cap:
            past = _t.time() - 2 * 3600                       # a COMPLETED hour, so compact() will touch it
            hd = _t.strftime("%Y-%m-%d", _t.gmtime(past))
            d = os.path.join(cap, hd, "M1", "book_snapshots")
            os.makedirs(d, exist_ok=True)
            base = int(past)
            true_rows = 0
            for k in range(3):                                # 3 parts × 10 rows = 30 true snapshots
                rows = [{"market_id": "M1", "token_id": "TK1", "capture_ts": base + k * 100 + i,
                         "exchange_ts": base + k * 100 + i} for i in range(10)]
                p = os.path.join(d, f"part-{k}.parquet")
                _pq.write_table(_pa.Table.from_pylist(rows), p)
                os.utime(p, (past, past))                     # backdate into the completed hour
                true_rows += len(rows)

            def _nsnaps():
                st = Store(capture_dir=cap, universe_ttl_s=0.0)   # ttl=0: always rescan (no stale cache)
                for (m, tk, _t0, _t1, n) in st.universe():
                    if m == "M1" and tk == "TK1":
                        return n
                return None

            n_pre = _nsnaps()
            s.check(n_pre == true_rows, "pre-compaction n_snaps == true rows", f"got {n_pre} exp {true_rows}")
            cg, cr = compact(cap)
            parts_left = _glob.glob(os.path.join(d, "part-*.parquet"))
            hour_files = _glob.glob(os.path.join(d, "hour-*.parquet"))
            s.check(cg == 1 and cr == 3 and not parts_left and len(hour_files) == 1,
                    "compact merged 3 parts into 1 hour file", f"groups={cg} removed={cr} left={len(parts_left)}")
            actual = sum(_pq.read_table(f).num_rows for f in hour_files + parts_left)
            n_post = _nsnaps()
            s.check(n_post == actual == true_rows,
                    "post-compaction n_snaps == actual parquet rows (no double-count)",
                    f"n_snaps={n_post} actual={actual} true={true_rows}")
    except Exception as e:
        s.check(False, "compaction_recount guard ran", repr(e))
    return s


def check_compactor_safety() -> Section:
    """HARD guard on the incremental compactor (desk/data/compactor.py) — the properties that make
    it safe to run against the LIVE capture store:
      • 10-min age floor: a part younger than MIN_AGE_S is never touched, judged by the FILENAME
        epoch (capture's own write stamp) even when the mtime lies old;
      • bounded passes: at most max_dirs leaf dirs per pass, oldest first, and the next pass
        continues where the last stopped;
      • crash leftovers: a crash after the hour-file rename (manifest + matching row count + parts
        still on disk) is recovered by deleting the now-duplicate parts; a crash before the rename
        (manifest + stale tmp, no matching target) is recovered by discarding the leftovers and
        re-running the merge — in BOTH cases total rows are conserved;
      • rerun-idempotent: a second pass over a compacted leaf is a no-op; a late part for an
        already-compacted hour folds into the existing hour file;
      • validate() never mutates (architecture review B19): a validator run over eligible parts
        leaves every file untouched and creates no hour files.
    Fully synthetic (temp dirs); no live store."""
    s = Section("compactor_safety", hard=True)
    try:
        import glob as _glob
        import tempfile
        import time as _t
        import pyarrow as _pa
        import pyarrow.parquet as _pq
        from desk.data.compactor import compact_pass
        from desk.data.capture_validate import validate
        from desk.types import CaptureConfig

        now = _t.time()
        # Fixture epochs are pinned to the START of the completed hour two hours ago. Spreading them
        # from a raw `now - 7200` made the section FLAKY: at XX:58:20–XX:59:59 UTC the +50/+100s
        # siblings landed in the NEXT hour, every by-hour group had <2 parts, and compact_pass
        # (correctly) merged nothing — the gate failed with no real defect.
        base = float((int(now) // 3600 - 2) * 3600)
        day = _t.strftime("%Y-%m-%d", _t.gmtime(base))
        hour = _t.strftime("%Y%m%d%H", _t.gmtime(base))

        def mkpart(d, epoch, idx, nrows):
            os.makedirs(d, exist_ok=True)
            rows = [{"market_id": "M", "token_id": "TK", "capture_ts": int(epoch) + i,
                     "exchange_ts": int(epoch) + i} for i in range(nrows)]
            p = os.path.join(d, f"part-1-{int(epoch)}-{idx}.parquet")
            _pq.write_table(_pa.Table.from_pylist(rows), p)
            return p

        def rows_in(d):
            return sum(_pq.ParquetFile(f).metadata.num_rows
                       for f in _glob.glob(os.path.join(d, "*.parquet")))

        # 1. age floor — filename epoch wins over a lying mtime
        with tempfile.TemporaryDirectory() as cap:
            d = os.path.join(cap, day, "M", "book_snapshots")
            mkpart(d, base, 0, 10)
            mkpart(d, base + 100, 1, 10)
            young = mkpart(d, now - 120, 2, 5)
            os.utime(young, (base, base))                    # backdated mtime must NOT defeat the floor
            st = compact_pass(cap, max_dirs=100)
            s.check(st["groups"] == 1 and st["parts_removed"] == 2 and os.path.exists(young)
                    and rows_in(d) == 25,
                    "10-min age floor holds (epoch from filename, mtime ignored)",
                    f"st={st} rows={rows_in(d)} young_kept={os.path.exists(young)}")

        # 2. bounded batch + oldest-first continuation
        with tempfile.TemporaryDirectory() as cap:
            for mi in range(5):
                d = os.path.join(cap, day, f"M{mi}", "book_snapshots")
                mkpart(d, base, 0, 3)
                mkpart(d, base + 100, 1, 3)
            st1 = compact_pass(cap, max_dirs=2)
            st2 = compact_pass(cap, max_dirs=100)
            total = sum(rows_in(os.path.join(cap, day, f"M{mi}", "book_snapshots")) for mi in range(5))
            s.check(st1["dirs_processed"] == 2 and st1["parts_removed"] == 4,
                    "pass compacts at most max_dirs leaf dirs", str(st1))
            s.check(st2["dirs_processed"] == 3 and total == 30,
                    "next pass continues; all rows conserved", f"st2={st2} total={total}")

        # 3a. crash AFTER rename, BEFORE part deletion (leaf manifest): listed parts are
        #     duplicates -> removed
        with tempfile.TemporaryDirectory() as cap:
            d = os.path.join(cap, day, "M", "book_snapshots")
            p1, p2 = mkpart(d, base, 0, 7), mkpart(d, base + 50, 1, 8)
            hour_f = os.path.join(d, f"hour-{hour}.parquet")
            _pq.write_table(_pa.concat_tables([_pq.read_table(p1), _pq.read_table(p2)]), hour_f)
            json.dump({"groups": [{"target": f"hour-{hour}.parquet", "expected_rows": 15,
                                   "parts": [os.path.basename(p1), os.path.basename(p2)]}]},
                      open(os.path.join(d, "compact.manifest.json"), "w"))
            st = compact_pass(cap, max_dirs=100)
            s.check(st["recovered_parts"] == 2 and st["recovered_manifests"] == 1
                    and not os.path.exists(p1) and not os.path.exists(p2) and rows_in(d) == 15
                    and not _glob.glob(os.path.join(d, "*manifest.json")),
                    "post-rename crash: duplicate parts removed, rows conserved",
                    f"st={st} rows={rows_in(d)}")

        # 3b. crash BEFORE rename (legacy per-group manifest shape): tmp+manifest discarded,
        #     merge re-runs, rows conserved
        with tempfile.TemporaryDirectory() as cap:
            d = os.path.join(cap, day, "M", "book_snapshots")
            p1, p2 = mkpart(d, base, 0, 7), mkpart(d, base + 50, 1, 8)
            open(os.path.join(d, f"hour-{hour}.parquet.tmp"), "w").write("partial-garbage")
            json.dump({"target": f"hour-{hour}.parquet", "expected_rows": 15,
                       "parts": [os.path.basename(p1), os.path.basename(p2)]},
                      open(os.path.join(d, f"hour-{hour}.manifest.json"), "w"))
            st = compact_pass(cap, max_dirs=100)
            left = _glob.glob(os.path.join(d, "*.tmp")) + _glob.glob(os.path.join(d, "*.manifest.json"))
            s.check(st["groups"] == 1 and rows_in(d) == 15 and not left
                    and not os.path.exists(p1) and not os.path.exists(p2),
                    "pre-rename crash: leftovers cleaned, merge re-ran, rows conserved",
                    f"st={st} rows={rows_in(d)} left={left}")

        # 4. rerun-idempotent + late part folds into the existing hour file
        with tempfile.TemporaryDirectory() as cap:
            d = os.path.join(cap, day, "M", "book_snapshots")
            mkpart(d, base, 0, 7)
            mkpart(d, base + 50, 1, 8)
            compact_pass(cap, max_dirs=100)
            st_noop = compact_pass(cap, max_dirs=100)
            mkpart(d, base + 100, 0, 4)                      # late write into a compacted hour
            st_fold = compact_pass(cap, max_dirs=100)
            nfiles = len(_glob.glob(os.path.join(d, "*.parquet")))
            s.check(st_noop["groups"] == 0 and st_noop["dirs_processed"] == 0,
                    "second pass over a compacted leaf is a no-op", str(st_noop))
            s.check(st_fold["groups"] == 1 and rows_in(d) == 19 and nfiles == 1,
                    "late part folds into the existing hour file (rows conserved)",
                    f"st={st_fold} rows={rows_in(d)} files={nfiles}")

        # 5. validate() never mutates (B19 split): eligible parts untouched, no hour files appear
        with tempfile.TemporaryDirectory() as cap:
            d = os.path.join(cap, day, "M", "book_snapshots")
            for k in range(3):
                rows = [{"market_id": "M", "token_id": "TK", "capture_ts": int(now - 7200) + k * 100 + i,
                         "exchange_ts": int(now - 7200) + k * 100 + i, "hash": "h",
                         "bids": [[0.4, 10.0]], "asks": [[0.6, 10.0]], "best_bid": 0.4,
                         "best_ask": 0.6, "mid": 0.5, "spread": 0.2, "tick_size": 0.01,
                         "min_order_size": 5.0, "neg_risk": False, "last_trade_price": 0.5,
                         "fee_rate_base": 1000.0, "fidelity_tier": "T5"} for i in range(10)]
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, f"part-{k}.parquet")
                _pq.write_table(_pa.Table.from_pylist(rows), p)
                os.utime(p, (now - 7200, now - 7200))        # old enough that a compactor WOULD merge
            before = sorted(_glob.glob(os.path.join(d, "*")))
            validate(CaptureConfig(out_dir=cap))
            after = sorted(_glob.glob(os.path.join(d, "*")))
            s.check(before == after and not _glob.glob(os.path.join(d, "hour-*.parquet")),
                    "validate() mutates nothing (compaction fully out of the validator)",
                    f"before={len(before)} after={len(after)}")
    except Exception as e:
        s.check(False, "compactor_safety guard ran", repr(e))
    return s


def check_settlement_chain() -> Section:
    """HARD regression: the T0 settlement chain against REAL Gamma shapes (the class that silently
    killed every network settle). Fully offline — the Gamma HTTP call is a stub session, the CLOB
    fallback is a stub function, the store runs on a temp dir:
      • a closed Gamma market whose endDate is an ISO-8601 STRING (what Gamma actually returns for
        closed markets) must parse through Sources.resolution — a bare int(endDate) raised and
        aborted resolution before the CLOB fallback could ever run;
      • an unparseable endDate still settles (resolved_ts falls to 0; the payout is the truth);
      • a Gamma failure falls through to the CLOB fallback (the chain, not just the first hop);
      • the store's resolutions cache invalidates on append (mtime/size) so a long-running driver
        sees resolutions recorded AFTER its first settle pass;
      • binary-payout hygiene both ways: non-{0,1} payout rows are neither written
        (capture_select._resolution_rows) nor accepted on read, and a None payout is never
        coerced to 0.0."""
    s = Section("settlement_chain", hard=True)
    try:
        import calendar as _cal
        import tempfile
        from desk.data.sources import Sources
        from desk.data.store import Store
        from desk.data.capture_select import _resolution_rows

        class _FakeResp:
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        class _FakeSession:
            def __init__(self, payload): self._p = payload
            def get(self, url, params=None, timeout=None):
                if isinstance(self._p, Exception):
                    raise self._p
                return _FakeResp(self._p)

        def _gamma_mkt(end_date):
            # real Gamma /markets shape: JSON-ENCODED-STRING arrays, ISO endDate on closed markets
            return {"conditionId": "0xc0ffee", "closed": True, "endDate": end_date,
                    "clobTokenIds": '["TKYES", "TKNO"]', "outcomePrices": '["1", "0"]',
                    "outcomes": '["Yes", "No"]'}

        # ── Gamma leg: ISO endDate parses; winner/loser payouts; unparseable date still settles ──
        src = Sources(session=_FakeSession([_gamma_mkt("2026-10-20T00:00:00Z")]))
        res = src.resolution("0xc0ffee", "TKYES")
        s.check(res is not None and res.payout == 1.0 and res.source == "GAMMA",
                "ISO endDate: closed Gamma market settles the winning token", repr(res))
        exp_ts = _cal.timegm((2026, 10, 20, 0, 0, 0))
        s.check(res is not None and res.resolved_ts == exp_ts,
                "ISO endDate parsed to epoch (trailing Z)", f"got {getattr(res, 'resolved_ts', None)} exp {exp_ts}")
        res_no = src.resolution("0xc0ffee", "TKNO")
        s.check(res_no is not None and res_no.payout == 0.0, "losing token settles 0.0", repr(res_no))
        res_bad = Sources(session=_FakeSession([_gamma_mkt("not-a-date")])).resolution("0xc0ffee", "TKYES")
        s.check(res_bad is not None and res_bad.payout == 1.0 and res_bad.resolved_ts == 0,
                "unparseable endDate -> resolved_ts 0, settle still happens", repr(res_bad))

        # ── chain: Gamma down -> CLOB fallback actually runs ────────────────
        import desk.data.market_feed as MF
        _real_clob_res = MF.clob_resolution
        MF.clob_resolution = lambda cond, tok: 1.0
        try:
            res_fb = Sources(session=_FakeSession(RuntimeError("gamma down"))).resolution("0xc0ffee", "TKYES")
        finally:
            MF.clob_resolution = _real_clob_res
        s.check(res_fb is not None and res_fb.payout == 1.0 and res_fb.source == "CLOB",
                "Gamma failure falls through to the CLOB fallback", repr(res_fb))

        # ── store: append-invalidated cache + binary-only read ──────────────
        with tempfile.TemporaryDirectory() as cap:
            st = Store(capture_dir=cap)                        # sources=None -> local jsonl only
            rpath = os.path.join(cap, "resolutions.jsonl")
            def _row(m, tk, payout):
                return json.dumps({"market_id": m, "token_id": tk, "resolved_ts": 1,
                                   "payout": payout, "outcome": "Yes", "source": "GAMMA"}) + "\n"
            with open(rpath, "w") as f:
                f.write(_row("M1", "T1", 1.0))
                f.write(_row("M2", "T2", 0.5))
                f.write(_row("M3", "T3", None))
            r1 = st.resolution("M1", "T1")
            s.check(r1 is not None and r1.payout == 1.0, "local jsonl settles a clean 1.0 row", repr(r1))
            s.check(st.resolution("M2", "T2") is None, "payout 0.5 row refused on read (not binary truth)")
            s.check(st.resolution("M3", "T3") is None, "payout None row refused — never coerced to 0.0")
            with open(rpath, "a") as f:                        # resolution lands AFTER the first settle pass
                f.write(_row("M4", "T4", 0.0))
            r4 = st.resolution("M4", "T4")
            s.check(r4 is not None and r4.payout == 0.0,
                    "cache invalidates on append (long-running driver sees new resolutions)", repr(r4))

        # ── writer: only exact-binary payouts enter resolutions.jsonl ───────
        base = {"clobTokenIds": '["A", "B"]', "outcomes": '["Yes", "No"]', "question": "q"}
        rows = _resolution_rows("0xc", {}, {**base, "outcomePrices": '["1", "0"]'})
        s.check(len(rows) == 2 and {r["payout"] for r in rows} == {1.0, 0.0},
                "writer emits both binary rows for a reported market", str(rows))
        s.check(_resolution_rows("0xc", {}, {**base, "outcomePrices": '["0.5", "0.5"]'}) == [],
                "writer skips a closed-but-unreported market (0.5/0.5)")
        s.check(_resolution_rows("0xc", {}, base) == [],
                "writer skips when outcomePrices are missing (no None payout rows)")
    except Exception as e:
        s.check(False, "settlement_chain fixtures ran", repr(e))
    return s


def check_driver_reload() -> Section:
    """HARD guard (control plane v2 §1): the driver re-reads the registry at the top of every loop
    iteration — flipping a row's `enabled` in the config file takes effect at the NEXT iteration
    WITHOUT a restart. Exercises strategy_driver._reload_specs against a real temp config file:
    flip-off drops the row from specs+due (no new entries from the next window), the OTHER row's
    due state is preserved by name, flip-back-on schedules at the NEXT window (never an immediate
    off-window fire), and a torn/mid-edit config read keeps the last good registry (no transient
    mass-drop/refire). Offline, store-free, seconds."""
    import tempfile
    import time as _time
    s = Section("driver_reload", hard=True)
    saved = os.environ.get("DESK_STRATEGY_CONFIG")
    try:
        from desk.signals import registry as R
        from desk.driver import strategy_driver as SD

        bases = R.base_specs()

        def _row(name):
            r = {"name": name, "strategy": "calendar", "enabled": True,
                 "schedule": {"type": "interval", "secs": 3600}, "params": {}}
            r["config_hash"] = R.seal_hash(r, bases["calendar"], R.base_version("calendar"))
            return r

        with tempfile.TemporaryDirectory() as td:
            cfgp = os.path.join(td, "strategy_config.json")

            def _write(rows):
                tmp = cfgp + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"strategies": rows}, f)
                os.replace(tmp, cfgp)

            ra, rb = _row("cal-a"), _row("cal-b")
            _write([ra, rb])
            os.environ["DESK_STRATEGY_CONFIG"] = cfgp
            specs = R.load_registry()
            s.check({x.name for x in specs} == {"cal-a", "cal-b"}, "both sealed rows load",
                    str(sorted(x.name for x in specs)))
            now = _time.time()
            due = {x.name: 123.0 for x in specs}      # pretend both already scheduled
            rb_off = dict(rb)
            rb_off["enabled"] = False
            _write([ra, rb_off])                      # ── mid-run pause: enabled -> false ──
            specs = SD._reload_specs(specs, due, now)
            s.check({x.name for x in specs} == {"cal-a"},
                    "enabled:false drops the row at the next iteration (no restart)",
                    str(sorted(x.name for x in specs)))
            s.check("cal-b" not in due and due.get("cal-a") == 123.0,
                    "due-map: dropped row removed, surviving row's schedule preserved by name", str(due))
            _write([ra, rb])                          # ── resume ──
            specs = SD._reload_specs(specs, due, now)
            s.check({x.name for x in specs} == {"cal-a", "cal-b"}, "re-enabled row reappears")
            s.check(due.get("cal-b", 0.0) >= now + 3599,
                    "re-enabled row scheduled at its NEXT window (no immediate off-window fire)",
                    f"due={due.get('cal-b')} now={now}")
            with open(cfgp, "w") as f:                # ── torn mid-edit read ──
                f.write('{"strategies": [')
            specs = SD._reload_specs(specs, due, now)
            s.check({x.name for x in specs} == {"cal-a", "cal-b"} and set(due) == {"cal-a", "cal-b"},
                    "torn config read keeps the last good registry (no mass drop)",
                    str(sorted(x.name for x in specs)))
    except Exception as e:
        s.check(False, "driver_reload guard ran", repr(e))
    finally:
        if saved is None:
            os.environ.pop("DESK_STRATEGY_CONFIG", None)
        else:
            os.environ["DESK_STRATEGY_CONFIG"] = saved
    return s


def check_exitmon_sweep() -> Section:
    """HARD guard (control plane v2 §2): the exit monitor sweeps EVERY on-disk positions_*.json
    regardless of the row's `enabled` flag — pause means "no new entries", never "no risk
    management". A DISABLED row's positions are swept with exit params resolved from that row; a
    positions file with NO config row is managed with the framework default TP/SL=none (loud); order
    placement keeps the live-only gating (live disarmed ⇒ read-only sweep, nothing posted, no books
    fetched). Fully offline: temp DRIVER_DIR + temp config, live disarmed."""
    import tempfile
    s = Section("exitmon_sweep", hard=True)
    saved_cfg = os.environ.get("DESK_STRATEGY_CONFIG")
    saved_live = os.environ.get("LIVE_TRADING_ENABLED")
    try:
        from desk.signals import registry as R
        from desk.driver import strategy_driver as SD
        from desk.driver import exit_monitor as XM
        bases = R.base_specs()
        row = {"name": "paused-arm", "strategy": "calendar", "enabled": False,
               "schedule": {"type": "interval", "secs": 3600},
               "params": {"live": True, "exit_tp": 0.2, "exit_sl": 0.5}}
        row["config_hash"] = R.seal_hash(row, bases["calendar"], R.base_version("calendar"))
        with tempfile.TemporaryDirectory() as td:
            cfgp = os.path.join(td, "cfg.json")
            with open(cfgp, "w") as f:
                json.dump({"strategies": [row]}, f)
            os.environ["DESK_STRATEGY_CONFIG"] = cfgp
            os.environ.pop("LIVE_TRADING_ENABLED", None)       # paper: placement must stay gated
            _real_dir = SD.DRIVER_DIR
            SD.DRIVER_DIR = td
            try:
                with open(os.path.join(td, "positions_paused-arm.json"), "w") as f:
                    json.dump({"M|TK": {"qty": 3.0, "entry_price": 0.4}}, f)
                with open(os.path.join(td, "positions_ghost-arm.json"), "w") as f:
                    json.dump({"M2|TK2": {"qty": 1.0, "entry_price": 0.5}}, f)
                out = XM.run_once()
                pa = out.get("paused-arm") or {}
                s.check(pa.get("held") == 1 and pa.get("enabled") is False,
                        "DISABLED arm's on-disk positions file is swept (held counted)", str(pa))
                s.check(pa.get("tp") == 0.2 and pa.get("sl") == 0.5,
                        "exit params resolved from the DISABLED config row", str(pa))
                ga = out.get("ghost-arm") or {}
                s.check(ga.get("held") == 1 and "MISSING" in str(ga.get("row", "")),
                        "row-less positions file managed with framework default TP/SL=none", str(ga))
                s.check(out.get("live_enabled") is False
                        and all("exits_submitted" not in (v or {}) for v in out.values()
                                if isinstance(v, dict)),
                        "placement gating intact: live disarmed ⇒ read-only sweep, nothing posted",
                        str(out))
            finally:
                SD.DRIVER_DIR = _real_dir
    except Exception as e:
        s.check(False, "exitmon_sweep guard ran", repr(e))
    finally:
        for k, v in (("DESK_STRATEGY_CONFIG", saved_cfg), ("LIVE_TRADING_ENABLED", saved_live)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def check_control_api() -> Section:
    """HARD guard (control plane v2 §3-5 / mission-control-v1 stage A): the dashboard control API.
    Real ThreadingHTTPServer on an ephemeral port, temp sealed calendar-variant config, temp
    DESK_DATA_DIR (audit) — fully offline, desk-only safe. Asserts: the CSRF floor (405 on GET to a
    mutating route, 403 on missing header / foreign Host), pause flips `enabled` atomically with an
    audit line + loader verify, resume mirrors it (hostile body fields ignored), live-routed resume
    without typed confirm ⇒ 409, pause-all flips every enabled row, no field but `enabled` ever
    changes, no-op flips write no audit line, loader-verify failure rolls the config back, seeding
    is one-entry-per-row + idempotent, effective_at mirrors the driver's scheduler, the control
    debounce 429s, and /api/health serves the full shape (arms = config ∪ on-disk positions, the
    v2 4-state modes, mode-since from the audit, THRESHOLDS verbatim), honoring its 30s cache
    with control flips invalidating it."""
    import copy
    import http.client
    import tempfile
    import threading
    import time as _time
    s = Section("control_api", hard=True)
    saved = {k: os.environ.get(k) for k in
             ("DESK_STRATEGY_CONFIG", "DESK_DATA_DIR", "LIVE_TRADING_ENABLED")}
    srv = None
    saved_hosts = saved_debounce = None
    DS = None
    try:
        from desk.signals import registry as R
        from desk import control_plane as CP
        import desk.dashboard_server as DS
        from desk.driver import strategy_driver as SD
        from desk.types import StrategySpec
        bases = R.base_specs()

        def _row(name, enabled, extra_params=None, schedule=None):
            r = {"name": name, "strategy": "calendar", "enabled": enabled,
                 "schedule": schedule or {"type": "interval", "secs": 3600},
                 "params": dict(extra_params or {})}
            r["config_hash"] = R.seal_hash(r, bases["calendar"], R.base_version("calendar"))
            return r

        with tempfile.TemporaryDirectory() as td:
            cfgp = os.path.join(td, "cfg.json")
            orig_rows = [_row("mc-a", True),
                         _row("mc-b", True, schedule={"type": "release_times",
                                                      "times_utc": ["00:30", "06:30",
                                                                    "12:30", "18:30"]}),
                         _row("mc-live", False, {"live": True})]
            with open(cfgp, "w") as f:
                json.dump({"strategies": orig_rows}, f, indent=1)
            orig_cfg = copy.deepcopy({"strategies": orig_rows})
            os.environ["DESK_STRATEGY_CONFIG"] = cfgp
            os.environ["DESK_DATA_DIR"] = td              # audit → <td>/audit/control_audit.jsonl
            os.environ.pop("LIVE_TRADING_ENABLED", None)

            # effective_at mirrors the driver's scheduler (the two implementations pinned together)
            now = _time.time()
            for sched in ({"type": "interval", "secs": 3600},
                          {"type": "release_times", "times_utc": ["00:30", "12:30"]}):
                spec = StrategySpec(name="x", schedule=sched, enabled=True, params={},
                                    generate_orders=None)
                s.check(abs(CP.next_fire_ts(sched, now) - SD._next_due(spec, now)) < 1.5,
                        f"effective_at mirrors strategy_driver._next_due :: {sched['type']}")

            # seeding: one entry per row, idempotent, carries effective_at
            n1, n2 = CP.seed_audit(), CP.seed_audit()
            s.check(n1 == 3 and n2 == 0, "seed writes one entry per config row, idempotent",
                    f"first={n1} second={n2}")
            seeds = CP.read_audit()
            s.check(all(e.get("seed") is True and e.get("action") == "seed"
                        and e.get("effective_at") for e in seeds)
                    and {e.get("arm") for e in seeds} == {"mc-a", "mc-b", "mc-live"},
                    "seed entries carry action/seed/effective_at per arm")

            # real server on an ephemeral port, allowlist + debounce adjusted for the test
            srv = DS.ThreadingHTTPServer(("127.0.0.1", 0), DS.Handler)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            saved_hosts, saved_debounce = DS.ALLOWED_HOSTS, DS.CONTROL_DEBOUNCE_S
            DS.ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
            DS.CONTROL_DEBOUNCE_S = 0.0

            def req(method, path, ctl=False, body=None, host=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                h = {"X-Desk-Control": "1"} if ctl else {}
                if host:
                    h["Host"] = host
                c.request(method, path,
                          body=(json.dumps(body).encode() if body is not None else None),
                          headers=h)
                r = c.getresponse()
                data = r.read()
                c.close()
                try:
                    return r.status, json.loads(data)
                except Exception:
                    return r.status, {}

            # CSRF floor
            s.check(req("GET", "/api/strategy/mc-a/pause")[0] == 405
                    and req("GET", "/api/stack/pause-all")[0] == 405,
                    "GET on a mutating control route ⇒ 405")
            s.check(req("POST", "/api/strategy/mc-a/pause")[0] == 403,
                    "POST without X-Desk-Control ⇒ 403")
            s.check(req("POST", "/api/strategy/mc-a/pause", ctl=True,
                        host="evil.example:80")[0] == 403,
                    "POST with a foreign Host ⇒ 403")
            s.check(json.load(open(cfgp)) == orig_cfg, "refused calls leave the config untouched")

            # pause: atomic enabled flip + audit + loader verify
            st, j = req("POST", "/api/strategy/mc-a/pause", ctl=True)
            cfg1 = json.load(open(cfgp))
            s.check(st == 200
                    and next(r for r in cfg1["strategies"] if r["name"] == "mc-a")["enabled"] is False,
                    "pause flips enabled → false", f"status={st}")
            s.check(CP._changed_paths(orig_cfg, cfg1) == ["strategies[0].enabled"],
                    "pause changed ONLY strategies[0].enabled",
                    str(CP._changed_paths(orig_cfg, cfg1)))
            last = CP.read_audit()[-1]
            s.check(last.get("action") == "pause" and last.get("arm") == "mc-a"
                    and last.get("prior") is True and last.get("new") is False
                    and last.get("field") == "enabled" and last.get("row_before")
                    and last.get("row_after"), "audit line: action/prior/new/row snapshots",
                    str(last)[:160])
            s.check(bool(j.get("results")) and j["results"][0].get("effective_at"),
                    "response carries effective_at")
            loaded = {sp.name for sp in R.load_registry(cfgp, include_disabled=True)}
            s.check({"mc-a", "mc-b", "mc-live"} <= loaded,
                    "loader still accepts every row after the flip")

            # no-op flip: no write, no audit line (mode-since must not reset)
            n_before = len(CP.read_audit())
            st, j = req("POST", "/api/strategy/mc-a/pause", ctl=True)
            s.check(st == 200 and j.get("noop") is True and len(CP.read_audit()) == n_before,
                    "no-op flip ⇒ 200 noop, NO audit line")

            # resume mirrors; hostile body fields are ignored (enabled-only edit)
            st, j = req("POST", "/api/strategy/mc-a/resume", ctl=True,
                        body={"live": True, "params": {"qty": 9}, "config_hash": "x"})
            cfg2 = json.load(open(cfgp))
            s.check(st == 200 and cfg2 == orig_cfg,
                    "resume mirrors pause; hostile body fields ignored — round-trip restores "
                    "the exact config", str(CP._changed_paths(orig_cfg, cfg2)))

            # live-routed resume: 409 without/with-wrong confirm, 200 with the exact name
            s.check(req("POST", "/api/strategy/mc-live/resume", ctl=True)[0] == 409
                    and req("POST", "/api/strategy/mc-live/resume", ctl=True,
                            body={"confirm_name": "nope"})[0] == 409,
                    "live-routed resume without exact confirm_name ⇒ 409")
            s.check(json.load(open(cfgp)) == orig_cfg, "409s leave the config untouched")
            st, j = req("POST", "/api/strategy/mc-live/resume", ctl=True,
                        body={"confirm_name": "mc-live"})
            cfg3 = json.load(open(cfgp))
            s.check(st == 200
                    and next(r for r in cfg3["strategies"] if r["name"] == "mc-live")["enabled"] is True,
                    "live-routed resume with typed confirm succeeds")

            # structural inability: across ALL calls, no field but `enabled` ever changed;
            # env untouched; the API offers no reseal (module-level assertion)
            for i, r0 in enumerate(orig_cfg["strategies"]):
                r1 = cfg3["strategies"][i]
                s.check(set(r1) == set(r0)
                        and all(r1[k] == r0[k] for k in r0 if k != "enabled"),
                        f"no field but enabled ever changed :: {r0['name']}")
            s.check(os.environ.get("LIVE_TRADING_ENABLED") is None,
                    "LIVE_TRADING_ENABLED untouched by the API")
            s.check(not any(hasattr(CP, a) for a in ("reseal", "seal", "set_live", "arm_live")),
                    "control plane exposes no reseal/arm-live surface")

            s.check(req("POST", "/api/strategy/ghost/pause", ctl=True)[0] == 404,
                    "unknown arm ⇒ 404")

            # pause-all: flips every enabled row (mc-a, mc-live), one audit line each
            n_before = len(CP.read_audit())
            st, j = req("POST", "/api/stack/pause-all", ctl=True)
            cfg4 = json.load(open(cfgp))
            s.check(st == 200 and all(r["enabled"] is False for r in cfg4["strategies"]),
                    "pause-all flips every enabled row to false")
            s.check(len(CP.read_audit()) == n_before + 3
                    and {e["arm"] for e in CP.read_audit()[-3:]} == {"mc-a", "mc-b", "mc-live"},
                    "pause-all audits one line per flipped arm",
                    f"lines={len(CP.read_audit()) - n_before} "
                    f"arms={sorted(e['arm'] for e in CP.read_audit()[-3:])}")

            # debounce: with a window armed, the next control POST 429s
            DS.CONTROL_DEBOUNCE_S = 60.0
            s.check(req("POST", "/api/strategy/mc-a/resume", ctl=True)[0] == 429,
                    "control debounce ⇒ 429")
            DS.CONTROL_DEBOUNCE_S = 0.0

            # loader-verify failure ⇒ 500 + byte rollback, nothing audited
            n_before = len(CP.read_audit())
            _real_lr = R.load_registry
            R.load_registry = lambda *a, **k: []
            try:
                raised = None
                try:
                    CP.flip_enabled(["mc-a"], True, "resume")
                except CP.ControlError as e:
                    raised = e
            finally:
                R.load_registry = _real_lr
            cfg5 = json.load(open(cfgp))
            s.check(raised is not None and raised.status == 500 and cfg5 == cfg4
                    and len(CP.read_audit()) == n_before,
                    "loader-verify failure ⇒ 500, config rolled back, nothing audited")

            # ── /api/health ───────────────────────────────────────────────────
            # state for derivation: mc-a resumed (PAPER), mc-live resumed (LIVE-LOCKED: live-routed,
            # gates unsatisfied), mc-b stays OFF; one residue positions file with NO config row.
            req("POST", "/api/strategy/mc-a/resume", ctl=True)
            req("POST", "/api/strategy/mc-live/resume", ctl=True, body={"confirm_name": "mc-live"})
            os.makedirs(os.path.join(td, "driver"), exist_ok=True)
            with open(os.path.join(td, "driver", "positions_mc-a.json"), "w") as f:
                json.dump({"M|TK": {"qty": 3.0, "entry_price": 0.4}}, f)
            with open(os.path.join(td, "driver", "positions_residue.json"), "w") as f:
                json.dump({"M2|TK2": {"qty": 1.0, "entry_price": 0.5}}, f)
            bo = DS._build_ops()
            if bo is None:
                s.check(req("GET", "/api/health")[0] == 503,
                        "health degrades to 503 without build_ops (skill not shipped)")
            else:
                saved_bo = (bo.DATA_HOME, bo.LOGS_DIR, bo.RPC_URL)
                bo.DATA_HOME = os.path.join(td, "home")
                bo.LOGS_DIR = os.path.join(bo.DATA_HOME, "logs")   # empty: no driver log yet
                bo.RPC_URL = "http://127.0.0.1:9/RPC2"             # closed port ⇒ degrade branch
                try:
                    DS._health_cache.update(ts=0.0, data=None)
                    st, h1 = req("GET", "/api/health")
                    s.check(st == 200, "/api/health ⇒ 200", f"status={st}")
                    s.check({"ts", "procs", "capture", "driver", "exitmon", "calib", "quota",
                             "pnl", "live_lock", "fire_in_progress", "arms", "thresholds"}
                            <= set(h1), "health payload carries the full shape",
                            str(sorted(h1))[:200])
                    s.check(h1["thresholds"] == {k: list(v) for k, v in bo.THRESHOLDS.items()},
                            "THRESHOLDS passed through verbatim")
                    s.check(h1["procs"]["up"] is False,
                            "supervisord-down degrades inside health (no raise)")
                    by = {a["name"]: a for a in h1["arms"]}
                    s.check({"mc-a", "mc-b", "mc-live", "residue"} <= set(by),
                            "arm rows = config rows ∪ on-disk positions", str(sorted(by)))
                    s.check(by["mc-a"]["mode"] == "PAPER" and by["mc-b"]["mode"] == "OFF"
                            and by["mc-live"]["mode"] == "LIVE-LOCKED"
                            and by["residue"]["mode"] == "OFF",
                            "4-state mode derivation (PAPER/OFF/LIVE-LOCKED/residue-OFF)",
                            str({k: v["mode"] for k, v in by.items()}))
                    s.check(CP.mode_of(True, True, True, True) == "LIVE",
                            "mode derivation: both gates satisfied ⇒ LIVE")
                    s.check(by["mc-a"]["since"] == CP.mode_since().get("mc-a")
                            and by["mc-a"]["since"] is not None,
                            "mode-since reads the arm's last audit flip")
                    s.check(by["mc-a"]["open_positions"] == 1
                            and by["residue"]["open_positions"] == 1
                            and by["residue"]["has_row"] is False,
                            "open positions counted; residue arm flagged has_row=false")
                    st, h2 = req("GET", "/api/health")
                    s.check(st == 200 and h2["ts"] == h1["ts"],
                            "30s cache honored (second call serves the same snapshot)")
                    req("POST", "/api/strategy/mc-a/pause", ctl=True)
                    st, h3 = req("GET", "/api/health")
                    by3 = {a["name"]: a for a in h3["arms"]}
                    # the fresh OFF (was PAPER) proves a recollect — ts alone is second-resolution
                    s.check(st == 200 and by3["mc-a"]["mode"] == "OFF",
                            "control flip invalidates the health cache (fresh mode visible)",
                            str(by3["mc-a"]["mode"]))

                    # ── stage B: the home route + /api/events + /api/week ──────────
                    import re as _re
                    hc = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    hc.request("GET", "/")
                    hr = hc.getresponse()
                    home = hr.read().decode(errors="ignore")
                    hc.close()
                    s.check(hr.status == 200 and 'id="cards"' in home and 'id="stripCells"' in home,
                            "home: GET / serves the Mission Control page")
                    s.check("/api/health" in home and "/api/events" in home and "/api/week" in home,
                            "home: hydrates from the three APIs")
                    s.check(_re.search(r"https?://", home) is None,
                            "home: zero external references (no absolute URLs at all)")
                    s.check("X-Desk-Control" in home and "confirm_name" in home,
                            "home: control POSTs carry the CSRF header; 409 typed-name path wired")
                    s.check('href="/index.html"' in home, "home: classic overview stays linked")
                    s.check('data-act="restart' not in home and "Arm live" not in home
                            and "set_live" not in home,
                            "home: no restart buttons, no live-arming affordance")
                    s.check("thresholds" in home, "home: popovers read health.thresholds")

                    st, e1 = req("GET", "/api/events")
                    s.check(st == 200 and isinstance(e1.get("events"), list) and "ts" in e1,
                            "/api/events ⇒ 200 with events[]", f"status={st}")
                    kinds = {e.get("kind") for e in e1["events"]}
                    s.check(kinds <= {"fire", "control", "process"},
                            "event kinds ⊆ {fire, control, process}", str(kinds))
                    tss = [e["ts"] for e in e1["events"]]
                    s.check(tss == sorted(tss, reverse=True), "events strictly newest-first")
                    s.check(not any(e.get("action") == "seed" for e in e1["events"]),
                            "audit seeds excluded from the feed")
                    st, e2 = req("GET", "/api/events")
                    s.check(st == 200 and e2["ts"] == e1["ts"],
                            "events 30s cache honored (same snapshot)")
                    req("POST", "/api/strategy/mc-a/resume", ctl=True)
                    st, e3 = req("GET", "/api/events")
                    s.check(st == 200 and any(e.get("kind") == "control" and e.get("arm") == "mc-a"
                                              and e.get("action") == "resume"
                                              for e in e3["events"][:3]),
                            "control flip invalidates the events cache; audit entry leads the feed")

                    st, w1 = req("GET", "/api/week")
                    today_utc = _time.strftime("%Y-%m-%d", _time.gmtime())
                    s.check(st == 200 and len(w1.get("days", [])) == 7
                            and w1["days"][-1]["date"] == today_utc,
                            "/api/week: 7 UTC days ending today")
                    s.check(all({"date", "realized", "closed", "by_arm"} <= set(d)
                                for d in w1["days"]),
                            "week day shape {date, realized, closed, by_arm}")
                    s.check(all(abs(sum(d["by_arm"].values()) - d["realized"]) < 1e-6
                                for d in w1["days"]),
                            "per-arm attribution sums to the day aggregate")
                    st, w2 = req("GET", "/api/week")
                    s.check(st == 200 and w2["ts"] == w1["ts"], "week 30s cache honored")

                    # rule lines: builtin describe() + schedule prefix; residue arms carry none;
                    # the labeled fallback covers tenants without the hook
                    st, h4 = req("GET", "/api/health")
                    by4 = {a["name"]: a for a in h4["arms"]}
                    s.check(isinstance(by4["mc-a"].get("rule"), str)
                            and "toy sample" in by4["mc-a"]["rule"]
                            and by4["mc-a"]["rule"].startswith("Every "),
                            "rule line = framework schedule prefix + describe()",
                            str(by4["mc-a"].get("rule"))[:90])
                    s.check(by4["residue"].get("rule") is None,
                            "residue arm (no config row) carries no rule line")
                    s.check("zero_fires" in by4["mc-a"]["today"],
                            "today counts carry zero-fire count")
                    fb = CP.describe_fallback({"budget": 80.0, "exit_tp": 0.2, "exit_sl": 0.5,
                                               "qty": 10.0, "private_x": 1})
                    s.check("$80 budget" in fb and "exits +20% / −50% at fire time" in fb
                            and "+1 strategy params" in fb,
                            "describe_fallback: labeled framework-read params", fb)

                    # ── stage C: GET /glossary renders from THRESHOLDS (live route) ──
                    hc = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    hc.request("GET", "/glossary")
                    gr = hc.getresponse()
                    gpage = gr.read().decode(errors="ignore")
                    hc.close()
                    s.check(gr.status == 200 and all(k in gpage for k in bo.THRESHOLDS),
                            "glossary route: 200 + every THRESHOLDS key present",
                            f"status={gr.status} missing="
                            f"{[k for k in bo.THRESHOLDS if k not in gpage]}")
                    s.check(_re.search(r"https?://", gpage) is None,
                            "glossary: zero external references")
                finally:
                    bo.DATA_HOME, bo.LOGS_DIR, bo.RPC_URL = saved_bo

    except Exception as e:
        s.check(False, "control_api guard ran", repr(e)[:200])
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        if DS is not None and saved_hosts is not None:
            DS.ALLOWED_HOSTS = saved_hosts
            DS.CONTROL_DEBOUNCE_S = saved_debounce
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def check_notifier() -> Section:
    """HARD guard (mission-control-v1 stage C): the red-alert notifier. Pure condition logic on
    SYNTHETIC health payloads — no network, no real state: one push per condition TRANSITION with
    a recovery push on clear; the book-flow-stall signature (fresh capture heartbeat, flat
    book_rows — the 2026-10-27 Zscaler/proxy incident) alerts after 2 unchanged consecutive checks
    and is suppressed only when supervisord says capture is deliberately stopped; substrate alerts
    inside the 2d window before its fail-closed gate; arm errors push WITHOUT the error text (no
    order/market detail leaves the machine); PASS expiry alerts under 3d and never without a PASS;
    /api/health unreachable alerts on the 2nd consecutive failure and HOLDS other conditions;
    messages carry value + threshold + the glossary action; the ntfy topic self-generates ONCE;
    and ops/supervisord.conf ships [program:notifier] standalone — NOT in [group:desk], so
    `reread` + `add notifier` can never touch the trading stack."""
    import tempfile
    import time as _time
    s = Section("notifier", hard=True)
    saved = {k: os.environ.get(k) for k in ("DESK_DATA_DIR", "NTFY_TOPIC")}
    try:
        import desk.notifier as N
        from desk import glossary as G
        TH = {"capture_hb": [300, 1800], "parquet": [600, 3600], "universe": [7200, 21600],
              "driver_hb": [300, 1800], "exitmon_pass": [600, 1800],
              "market_meta": [129600, 259200], "substrate_series": [259200, 604800],
              "quota_pct": [70, 1000]}
        now = _time.time()

        def day(off):
            return _time.strftime("%Y-%m-%d", _time.gmtime(now + off * 86400))

        def payload(**over):
            h = {"ts": int(now), "thresholds": TH,
                 "procs": {"up": True, "rows": [{"name": "desk:capture", "state": "RUNNING"}]},
                 "capture": {"hb_age_s": 20.0, "book_rows": 1000},
                 "driver": {"hb_age_s": 10.0},
                 "exitmon": {"lastpass_age_s": 100.0},
                 "calib": {"tenant": "x", "rows": [
                     {"kind": "series", "label": "s1", "newest": day(-2), "chip": "ok"}]},
                 "live_lock": {"env": False, "pass": None},
                 "arms": [{"name": "a1", "last_error": None}]}
            h.update(over)
            return h

        pushes = []

        def send(kind, cid, msg):
            pushes.append((kind, cid, msg))

        with tempfile.TemporaryDirectory() as td:
            os.environ["DESK_DATA_DIR"] = td
            os.environ.pop("NTFY_TOPIC", None)

            st = {}
            N.check_once(st, lambda: payload(), send)
            s.check(not pushes and not st["active"], "healthy payload ⇒ no conditions, no pushes")

            stale = payload(capture={"hb_age_s": 2000.0, "book_rows": 1000})
            N.check_once(st, lambda: stale, send)
            N.check_once(st, lambda: stale, send)
            s.check([p[:2] for p in pushes] == [("alert", "capture_stale")],
                    "capture hb > red ⇒ ONE alert across repeated loops (transition, not loop)",
                    str([p[:2] for p in pushes]))
            msg = pushes[0][2] if pushes else ""
            s.check(G.fmt_span(2000) in msg and G.fmt_span(TH["capture_hb"][1]) in msg
                    and G.action_for("capture_hb") in msg,
                    "alert message = value + threshold + glossary action", msg[:140])
            N.check_once(st, lambda: payload(capture={"hb_age_s": 5.0, "book_rows": 1001}), send)
            s.check(pushes[-1][:2] == ("recovered", "capture_stale"),
                    "recovery push when the condition clears")

            # the book-flow-stall signature (Zscaler/proxy, 2026-10-27)
            pushes.clear()
            st = {}
            for _ in range(3):
                N.check_once(st, lambda: payload(), send)
            s.check([p[:2] for p in pushes] == [("alert", "book_flow_stalled")],
                    "fresh heartbeat + book_rows flat across 2 consecutive checks ⇒ stall alert, "
                    "exactly once, on the 3rd observation", str([p[:2] for p in pushes]))
            s.check(bool(pushes) and "1000" in pushes[0][2] and "book_rows" in pushes[0][2],
                    "stall message names the stuck counter")
            N.check_once(st, lambda: payload(capture={"hb_age_s": 20.0, "book_rows": 2000}), send)
            s.check(pushes[-1][:2] == ("recovered", "book_flow_stalled"),
                    "moving book_rows clears the stall")
            pushes.clear()
            st = {}
            stopped = {"up": True, "rows": [{"name": "desk:capture", "state": "STOPPED"}]}
            for _ in range(3):
                N.check_once(st, lambda: payload(procs=stopped), send)
            s.check(not pushes,
                    "stall suppressed when supervisord says capture is deliberately stopped")
            pushes.clear()
            st = {}
            for _ in range(3):
                N.check_once(st, lambda: payload(
                    capture={"hb_age_s": 2000.0, "book_rows": 1000}), send)
            s.check(all(p[1] != "book_flow_stalled" for p in pushes)
                    and any(p[1] == "capture_stale" for p in pushes),
                    "stale heartbeat ⇒ capture_stale, never a stall alert on top")

            # driver + exit-monitor red (missing counts as stale — unknown is not healthy)
            pushes.clear()
            st = {}
            N.check_once(st, lambda: payload(driver={"hb_age_s": 1801.0},
                                             exitmon={"lastpass_age_s": None}), send)
            s.check({p[1] for p in pushes} == {"driver_stale", "exitmon_stale"},
                    "driver > red + exitmon missing ⇒ both alerts",
                    str(sorted(p[1] for p in pushes)))

            # substrate: inside the 2d pre-fail-closed window; MISSING required series
            pushes.clear()
            st = {}
            cal = {"tenant": "x", "rows": [
                {"kind": "series", "label": "s1", "newest": day(-6), "chip": "warn"},
                {"kind": "series", "label": "s2", "value": "MISSING", "chip": "bad"},
                {"kind": "meta", "label": "m", "newest": None, "chip": "bad"}]}
            N.check_once(st, lambda: payload(calib=cal), send)
            s.check({p[1] for p in pushes} == {"substrate:s1", "substrate:s2"},
                    "substrate 6d old (gate 7d, window 2d) + MISSING series alert; meta rows do "
                    "not", str(sorted(p[1] for p in pushes)))
            pushes.clear()
            st = {}
            N.check_once(st, lambda: payload(), send)
            s.check(not pushes, "substrate 2d old (outside the window) ⇒ quiet")

            # arm error: per-arm alert, error TEXT never leaves the machine
            pushes.clear()
            st = {}
            N.check_once(st, lambda: payload(arms=[{"name": "a1", "last_error": {
                "ts": int(now), "error": "RuntimeError order 0xDEAD qty=5 px=0.44"}}]), send)
            s.check([p[1] for p in pushes] == ["arm_error:a1"], "arm last_error ⇒ per-arm alert")
            s.check(bool(pushes) and "0xDEAD" not in pushes[0][2] and "qty" not in pushes[0][2]
                    and "0.44" not in pushes[0][2] and "a1" in pushes[0][2],
                    "error/order detail withheld from the push (names the arm only)",
                    pushes[0][2] if pushes else "")

            # PASS expiry — only when a PASS exists, only under 3d
            pushes.clear()
            st = {}

            def mkpass(exp):
                return {"scope": ["taker"], "strategies": ["sX"], "market_class": "weather",
                        "granted": day(-20), "expiry": exp}

            N.check_once(st, lambda: payload(
                live_lock={"env": True, "pass": mkpass(day(10))}), send)
            s.check(not pushes, "PASS with ~10d left ⇒ quiet (and no-PASS ⇒ quiet, above)")
            N.check_once(st, lambda: payload(
                live_lock={"env": True, "pass": mkpass(day(2))}), send)
            s.check([p[1] for p in pushes] == ["pass_expiring"] and "weather" in pushes[0][2],
                    "PASS under 3d ⇒ alert naming scope + expiry")

            # the dashboard-dead blind spot: 2 consecutive failures, prior conditions held
            pushes.clear()
            st = {}
            N.check_once(st, lambda: None, send)
            s.check(not pushes, "first /api/health failure ⇒ no alert yet")
            N.check_once(st, lambda: None, send)
            s.check([p[:2] for p in pushes] == [("alert", "health_unreachable")],
                    "second consecutive failure ⇒ the blind-spot alert")
            st2 = {"active": {"capture_stale": {"since": 1, "msg": "x"}}, "health_fails": 0}
            N.check_once(st2, lambda: None, send)
            s.check("capture_stale" in st2["active"],
                    "an outage HOLDS prior conditions (no false recovery while blind)")
            N.check_once(st, lambda: payload(), send)
            s.check(pushes[-1][:2] == ("recovered", "health_unreachable"),
                    "reachable again ⇒ recovery push")

            # state persistence + topic generation (no secrets committed anywhere)
            N.save_state(st)
            s.check(N.load_state() == st and N.state_path().startswith(td),
                    "state round-trips at $DESK_DATA_DIR/notifier_state.json")
            t1, t2 = N.ensure_ntfy_topic(), N.ensure_ntfy_topic()
            s.check(t1 == t2 and t1.startswith("polyedge-") and len(t1) >= 30
                    and f"NTFY_TOPIC={t1}" in open(N.env_file_path()).read(),
                    "ntfy topic: long random default generated ONCE into config/notifier.env")
            os.environ.pop("NTFY_TOPIC", None)
            s.check(N.push_ntfy("t", "m", "high", "x") is False,
                    "no topic configured ⇒ no push attempted (never a surprise destination)")

        # supervisord wiring: standalone program, autostart+autorestart, logs, NOT in group desk
        conf = open(os.path.join(REPO_ROOT, "ops", "supervisord.conf")).read()
        s.check("[program:notifier]" in conf, "supervisord.conf ships [program:notifier]")
        block = conf.split("[program:notifier]", 1)[1].split("\n[", 1)[0] \
            if "[program:notifier]" in conf else ""
        s.check("-m desk.notifier" in block, "notifier runs the committed -m desk.notifier")
        s.check("autostart=true" in block and "autorestart=true" in block,
                "notifier autostart + autorestart")
        s.check("logs/notifier.out.log" in block, "notifier logs under polyedge-data/logs")
        grp = [ln for ln in conf.splitlines() if ln.strip().startswith("programs=")]
        s.check(bool(grp) and all("notifier" not in g for g in grp),
                "notifier NOT in [group:desk] — reread+add can never touch the trading stack",
                grp[0] if grp else "no group line")
    except Exception as e:
        s.check(False, "notifier guard ran", repr(e)[:200])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def check_exit_rules() -> Section:
    """HARD guard (framework): the generic TP/SL exit builder's sign/side matrix — long TP/SL -> SELL,
    short TP/SL -> BUY back, no trigger inside the band, and the decision price is the REALIZABLE touch
    (bid for longs), not the mid. Desk-only safe (no tenant imports); tenants add behavioral fixtures
    around the same builder via their eval_checks hook."""
    s = Section("exit_rules", hard=True)
    try:
        from types import SimpleNamespace as NS
        from desk.driver.exit_rules import tpsl_exit_order
        mk = lambda q, ep: {"qty": q, "entry_price": ep}
        o = tpsl_exit_order(mk(3.0, 0.40), NS(book=NS(best_bid=0.47, best_ask=0.49)),
                            "m", "tk", "sid", 1, 0.10, None)
        s.check(o is not None and o.side == "SELL" and abs(o.qty - 3.0) < 1e-9, "long TP -> SELL full qty",
                repr(o))
        o = tpsl_exit_order(mk(3.0, 0.40), NS(book=NS(best_bid=0.29, best_ask=0.31)),
                            "m", "tk", "sid", 1, None, 0.20)
        s.check(o is not None and o.side == "SELL", "long SL -> SELL")
        o = tpsl_exit_order(mk(-3.0, 0.60), NS(book=NS(best_bid=0.43, best_ask=0.45)),
                            "m", "tk", "sid", 1, 0.10, None)
        s.check(o is not None and o.side == "BUY", "short TP -> BUY back")
        o = tpsl_exit_order(mk(-3.0, 0.60), NS(book=NS(best_bid=0.74, best_ask=0.76)),
                            "m", "tk", "sid", 1, None, 0.30)
        s.check(o is not None and o.side == "BUY", "short SL -> BUY back")
        # realizable-touch (not mid): ret = (0.41-0.40)/0.40 = +2.5% sits inside (-sl 20%, +tp 10%).
        o = tpsl_exit_order(mk(3.0, 0.40), NS(book=NS(best_bid=0.41, best_ask=0.43)),
                            "m", "tk", "sid", 1, 0.10, 0.20)
        s.check(o is None, "inside the band -> no order")
        s.check(tpsl_exit_order({"qty": 0.0, "entry_price": 0.4}, NS(book=NS(best_bid=0.5, best_ask=0.6)),
                                "m", "tk", "sid", 1, 0.1, 0.1) is None, "flat position -> no order")
    except Exception as e:
        s.check(False, "exit_rules guard ran", repr(e))
    return s


def check_daily_cap() -> Section:
    """HARD guard (framework): MAX_DAILY_USD is a REAL enforced ledger, not an .env promise.
    Exercises the executor's daily-spend meter in an isolated DESK_DATA_DIR: off by default,
    reserve-under-cap accepted, over-cap refused, settle shrinks the reservation to the actual
    fill, UTC day key persisted, corrupt ledger ⇒ fail-closed refusal, malformed cap ⇒ raises.
    Desk-only safe, no network, no store."""
    import tempfile
    import time as _time
    s = Section("daily_cap", hard=True)
    saved = {k: os.environ.get(k) for k in ("MAX_DAILY_USD", "DESK_DATA_DIR")}
    try:
        from desk.live import executor as X
        with tempfile.TemporaryDirectory() as td:
            os.environ["DESK_DATA_DIR"] = td
            os.environ.pop("MAX_DAILY_USD", None)
            s.check(X.max_daily_usd() is None, "cap OFF by default (unset ⇒ None)")
            os.environ["MAX_DAILY_USD"] = "off"
            s.check(X.max_daily_usd() is None, "'off' ⇒ None")
            os.environ["MAX_DAILY_USD"] = "ten dollars"
            try:
                X.max_daily_usd()
                s.check(False, "malformed cap raises (fail-closed)", "no exception")
            except RuntimeError:
                s.check(True, "malformed cap raises (fail-closed)")
            os.environ["MAX_DAILY_USD"] = "10"
            s.check(X.max_daily_usd() == 10.0, "numeric cap parsed")
            ok, spent, day = X._reserve_daily_spend(6.0, 10.0)
            s.check(ok and spent == 0.0, "first reservation under cap accepted", f"ok={ok} spent={spent}")
            s.check(day == _time.strftime("%Y-%m-%d", _time.gmtime()), "ledger keyed by UTC day", day)
            ok2, spent2, _ = X._reserve_daily_spend(5.0, 10.0)
            s.check((not ok2) and abs(spent2 - 6.0) < 1e-9, "over-cap reservation REFUSED",
                    f"ok={ok2} spent={spent2}")
            X._settle_daily_spend(6.0, 2.5, day)          # only $2.50 actually filled
            s.check(abs(X.daily_spend(day) - 2.5) < 1e-9, "settle shrinks reservation to actual fill",
                    f"spend={X.daily_spend(day)}")
            ok3, _, _ = X._reserve_daily_spend(5.0, 10.0)
            s.check(ok3, "freed budget re-usable after settle")
            ledger = os.path.join(td, "driver", "daily_spend.json")
            s.check(os.path.exists(ledger), "ledger persisted under DESK_DATA_DIR/driver/")
            with open(ledger, "w") as f:
                f.write("{corrupt")
            ok4, spent4, _ = X._reserve_daily_spend(0.01, 10.0)
            s.check((not ok4) and spent4 < 0, "corrupt ledger ⇒ refuse (fail-closed, never fail-open)",
                    f"ok={ok4} spent={spent4}")
    except Exception as e:
        s.check(False, "daily-cap guard ran", repr(e))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def tenant_sections() -> List[Section]:
    """Gate sections contributed by discovered tenants (manifest hook `eval_checks`), labeled
    `<tenant>:<section>`. A desk-only tree (no tenants) SKIPS with the reason stated — never a
    failure; a PRESENT tenant whose declared hook cannot run IS a hard failure (its gate sections
    silently not running would be the fail-open class this mechanism exists to kill)."""
    out: List[Section] = []
    try:
        from desk.signals import registry as R
        tenants = R.discover_tenants()
    except Exception as e:
        s = Section("tenant_checks", hard=True)
        s.check(False, "tenant discovery ran", repr(e))
        return [s]
    if not tenants:
        s = Section("tenant_checks", hard=False)
        s.notes.append(f"no tenants under {R.strategies_dir()} — tenant sections skipped (desk-only tree)")
        return [s]
    for m in tenants:
        hook = (m.get("hooks") or {}).get("eval_checks")
        if not hook:
            s = Section(f"{m['name']}:eval_checks", hard=False)
            s.notes.append("tenant declares no eval_checks hook — nothing to run")
            out.append(s)
            continue
        try:
            secs = list(R.load_entrypoint(hook)())
        except Exception as e:
            s = Section(f"{m['name']}:eval_checks", hard=True)
            s.check(False, f"tenant eval_checks hook {hook!r} ran", repr(e))
            out.append(s)
            continue
        for sec in secs:
            sec.name = f"{m['name']}:{sec.name}"
            out.append(sec)
    return out


def check_ops_page() -> Section:
    """The /ops health page generator (.claude/skills/desk-dashboard/scripts/build_ops.py) must import
    cleanly, collect+render on EMPTY dirs without raising (fresh install / desk-only tree), degrade when
    supervisord is down (closed port ⇒ note, not crash), and keep its chip thresholds ordered. Absent
    script (a cut that drops the skill) ⇒ skipped WITH REASON — ops tooling absence isn't a trading bug."""
    s = Section("ops_page", hard=True)
    path = os.path.join(REPO_ROOT, ".claude", "skills", "desk-dashboard", "scripts", "build_ops.py")
    if not os.path.exists(path):
        s.notes.append("build_ops.py absent — ops-page checks skipped (skill not shipped in this tree)")
        return s
    import importlib.util
    import tempfile
    saved = {k: os.environ.get(k) for k in ("DESK_DATA_DIR", "DESK_STRATEGY_CONFIG")}
    try:
        spec = importlib.util.spec_from_file_location("_ops_page_gate", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        s.check(True, "build_ops imports cleanly")
        # thresholds sane: fresh=green, between=amber, beyond=red, unknown=grey
        s.check(mod.chip_age(0, 300, 1800) == mod.GREEN, "chip_age fresh ⇒ green")
        s.check(mod.chip_age(600, 300, 1800) == mod.AMBER, "chip_age mid ⇒ amber")
        s.check(mod.chip_age(3600, 300, 1800) == mod.RED, "chip_age stale ⇒ red")
        s.check(mod.chip_age(None, 300, 1800) == mod.GREY, "chip_age unknown ⇒ grey")
        # single-source thresholds (v2 glossary fix): chip logic AND tooltip text render from ONE
        # dict — kills the drifted hand-written-claim class ("stale >5min = red" on a 30-min chip).
        s.check(isinstance(getattr(mod, "THRESHOLDS", None), dict)
                and {"capture_hb", "driver_hb", "exitmon_pass"} <= set(mod.THRESHOLDS),
                "THRESHOLDS single-source dict present")
        s.check({"substrate_series", "quota_pct"} <= set(mod.THRESHOLDS),
                "substrate + quota thresholds live in THRESHOLDS (stage B)")
        s.check(mod.chip_for("substrate_series", 4 * 86400) == mod.AMBER
                and mod.chip_for("substrate_series", 8 * 86400) == mod.RED,
                "substrate chip judged from THRESHOLDS")
        _g, _r = mod.THRESHOLDS["capture_hb"]
        s.check(mod.chip_for("capture_hb", _g) == mod.GREEN
                and mod.chip_for("capture_hb", _r + 1) == mod.RED,
                "chip_for judges from THRESHOLDS")
        s.check(mod._fmt_age(_g) in mod.threshold_note("capture_hb")
                and mod._fmt_age(_r) in mod.threshold_note("capture_hb"),
                "tooltip threshold text renders from the SAME values")
        with tempfile.TemporaryDirectory() as td:
            os.environ["DESK_DATA_DIR"] = os.path.join(td, "data")      # empty runtime tree
            os.environ.pop("DESK_STRATEGY_CONFIG", None)
            mod.DATA_HOME = os.path.join(td, "home")                     # no logs dir yet
            mod.LOGS_DIR = os.path.join(mod.DATA_HOME, "logs")
            mod.RPC_URL = "http://127.0.0.1:9/RPC2"                      # closed port ⇒ degrade branch
            d = mod.collect()
            s.check(d["procs"]["up"] is False and d["procs"]["err"],
                    "supervisord-down degrades to err note (no raise)")
            html = mod.render(d)
            s.check("ops" in html and "Calibration substrate" in html and "refresh" in html,
                    "renders full page on EMPTY dirs")
            s.check(mod.threshold_note("capture_hb", "flushes every 30s") in html,
                    "capture tooltip IS the rendered threshold note (single source)")
            s.check("stale &gt;5min = red" not in html,
                    "the drifted hand-written capture claim is gone")
            s.check(d["capture"]["hb_age_s"] is None and d["quota"]["used"] == 0,
                    "empty dirs read as unknown/zero, never fake-fresh")
            # quota chip derives from THRESHOLDS["quota_pct"]: write a meter at 80% ⇒ amber
            import time as _t
            dd = os.path.join(td, "data", "data")
            os.makedirs(dd, exist_ok=True)
            with open(os.path.join(dd, "live_forecast_cache.json"), "w") as f:
                json.dump({"__meter__": {"day": _t.strftime("%Y-%m-%d", _t.gmtime()),
                                         "weighted": int(mod.OM_CAP * 0.8)}}, f)
            q = mod.collect_quota()
            s.check(q["chip"] == mod.AMBER and q["pct"] >= mod.THRESHOLDS["quota_pct"][0],
                    "quota chip ambers from THRESHOLDS quota_pct", str(q["pct"]))
            # P0 2026-10-27: a meter capped YESTERDAY read as "0% used" with no trace of the cap —
            # the stale meter row must stay visible (meter_day/capped_on + a note), never silent.
            with open(os.path.join(dd, "live_forecast_cache.json"), "w") as f:
                json.dump({"__meter__": {"day": "2001-01-01", "weighted": mod.OM_CAP,
                                         "cap_logged": "2001-01-01"}}, f)
            q = mod.collect_quota()
            s.check(q["used"] == 0 and q["meter_day"] == "2001-01-01"
                    and q["capped_on"] == "2001-01-01" and "2001-01-01" in q["note"],
                    "stale capped meter surfaces meter_day/capped_on + note (never a bare 0%)", str(q))

            # ── stage C: PASS panel + actions-history shape + glossary from THRESHOLDS ──
            from desk import control_plane as _CP
            _CP.append_audit({"ts": 100, "action": "seed", "seed": True, "arm": "a",
                              "field": "enabled", "prior": None, "new": False, "source_ip": None,
                              "ua": None, "effective_at": 90, "row_before": None, "row_after": {}})
            _CP.append_audit({"ts": 200, "action": "pause", "arm": "a", "field": "enabled",
                              "prior": True, "new": False, "source_ip": "127.0.0.1", "ua": "ua",
                              "effective_at": 260, "row_before": {}, "row_after": {}})
            d2 = mod.collect()
            s.check({"pass_gates", "audit"} <= set(d2), "collect() carries pass_gates + audit")
            s.check(d2["audit"]["total"] == 2
                    and [r["action"] for r in d2["audit"]["rows"]] == ["pause", "seed"],
                    "audit collector reads the JSONL newest-first",
                    str([r.get("action") for r in d2["audit"]["rows"]]))
            d2["pass_gates"] = {"env": False, "pass": None, "err": None}
            html2 = mod.render(d2)
            s.check("Live unlock (PASS)" in html2 and "none — live locked" in html2,
                    "ops renders the PASS panel — locked wording when no PASS")
            s.check("Control actions history" in html2 and "(bootstrap)" in html2
                    and "on → off" in html2 and "data-ts='200'" in html2
                    and html2.index("data-ts='200'") < html2.index("data-ts='100'"),
                    "actions history: newest-first, seeds labeled, prior→new, effective-at")
            s.check("tzbtn" in html2, "DESK_TZ display toggle present (storage stays UTC)")
            d2["pass_gates"] = {"env": False, "err": None,
                                "pass": {"scope": ["taker"], "strategies": ["armx"],
                                         "market_class": "weather", "granted": "2026-10-20",
                                         "n_fills": 41,
                                         "expiry": _t.strftime(
                                             "%Y-%m-%d", _t.gmtime(_t.time() + 12 * 86400))}}
            html3 = mod.render(d2)
            s.check("PASS: taker · armx · weather ·" in html3 and "d left" in html3,
                    "PASS panel shows SCOPE + EXPIRY (v2 format), never a bare countdown")
            from desk import glossary as _G
            gpage = _G.render_glossary(mod.THRESHOLDS)
            s.check(all(k in gpage for k in mod.THRESHOLDS),
                    "glossary renders EVERY THRESHOLDS key",
                    str([k for k in mod.THRESHOLDS if k not in gpage]))
            _cg, _cr = mod.THRESHOLDS["capture_hb"]
            s.check(_G.fmt_span(_cg) in gpage and _G.fmt_span(_cr) in gpage,
                    "glossary capture thresholds render FROM the dict (corrected text)")
            s.check("5min = red" not in gpage and "stale &gt;5min" not in gpage,
                    "the drifted hand-written capture claim cannot reappear in the glossary")
            s.check("Live unlock" in gpage and "scope" in gpage and "expires" in gpage,
                    "glossary live-unlock entry carries scope + expiry wording")
    except Exception as e:
        s.check(False, "ops-page generator check", repr(e)[:160])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def check_mcp_server() -> Section:
    """HARD guard (MCP v1): the desk's MCP server — the AI-native surface must stay EXACTLY the
    nine read/explain/alert/pause tools (no live-arming, no orders, no config/seal mutation), and
    its control flips must travel the AUDITED HTTP path (X-Desk-Control), never the config-writing
    internals. Asserts: exact tool inventory + arg schemas; AST-level import ban (the only desk
    import is desk.glossary — no control_plane/signals/dashboard_server/driver/live) + no
    config-writing identifiers; docs/mcp.md exists, names every tool, carries the claude-mcp-add
    line and the exclusions; and behavior against a STUB dashboard: health passthrough verbatim
    (trim nothing), events limit clamp, strategies = arms rows, explain fuzzy lookup rendering
    thresholds from the live payload, alerts parsed from the notifier state file, the control
    header on every flip, 409 typed-confirm and 429 debounce bodies surfaced VERBATIM, and a dead
    dashboard degrading to an error dict (never a raise). Fully offline, desk-only safe."""
    import ast
    import asyncio
    import http.server
    import re as _re
    import tempfile
    import threading
    import time as _time
    s = Section("mcp_server", hard=True)
    EXPECTED = {"desk_health", "desk_events", "desk_week", "desk_strategies", "desk_explain",
                "desk_alerts", "desk_pause", "desk_resume", "desk_pause_all"}
    saved = {k: os.environ.get(k) for k in ("DESK_DASH_URL", "DESK_DATA_DIR")}
    stub = None
    try:
        import desk.mcp_server as MS

        # ── exact tool inventory + schemas (the MCP layer itself) ─────────────
        tools = asyncio.run(MS.server.list_tools())
        names = {t.name for t in tools}
        s.check(names == EXPECTED and len(tools) == 9,
                "tool inventory EXACT: the nine desk tools, nothing else",
                f"got={sorted(names)}")
        by = {t.name: t for t in tools}
        if names == EXPECTED:
            s.check(by["desk_events"].input_schema["properties"]["limit"].get("default") == 50,
                    "desk_events schema: limit (default 50)")
            s.check(by["desk_pause"].input_schema.get("required") == ["name"],
                    "desk_pause schema: name required")
            rs = by["desk_resume"].input_schema
            s.check(rs.get("required") == ["name"] and "confirm_name" in rs["properties"],
                    "desk_resume schema: name required + optional confirm_name")
            s.check(by["desk_explain"].input_schema.get("required") == ["term"],
                    "desk_explain schema: term required")
            s.check(all(not by[n].input_schema.get("required")
                        for n in ("desk_health", "desk_week", "desk_strategies",
                                  "desk_alerts", "desk_pause_all")),
                    "read/pause-all tools take no required args")
            s.check(all((t.description or "").strip() for t in tools),
                    "every tool carries a description")

        # ── static: control flips ONLY via HTTP — never the config-writing internals ──
        src_path = MS.__file__.replace(".pyc", ".py")
        with open(src_path) as f:
            src = f.read()
        tree = ast.parse(src)
        desk_imports: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                desk_imports += [a.name for a in node.names if a.name.split(".")[0] == "desk"]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] == "desk":
                    desk_imports += [f"{mod}.{a.name}" for a in node.names]
        s.check(set(desk_imports) <= {"desk.glossary"},
                "imports from desk.* = desk.glossary ONLY (read-only vocabulary)",
                str(desk_imports))
        idents = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
            | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        forbidden = {"flip_enabled", "pause_all", "append_audit", "seed_audit",
                     "_write_config_bytes", "load_registry", "seal_hash", "config_path"}
        s.check(not (idents & forbidden),
                "no config-writing identifier referenced", str(sorted(idents & forbidden)))
        s.check("X-Desk-Control" in src and "/api/strategy/" in src
                and "/api/stack/pause-all" in src,
                "flips go through the audited HTTP control API (header + routes in source)")
        s.check("no live-arming" in src.lower() and "order placement" in src.lower(),
                "module docstring states the exclusions")

        # ── behavior against a stub dashboard (offline) ───────────────────────
        HEALTH = {"ts": 111, "procs": {"up": True, "rows": []},
                  "arms": [{"name": "mc-a", "mode": "PAPER", "enabled": True},
                           {"name": "mc-live", "mode": "LIVE-LOCKED", "enabled": True}],
                  "thresholds": {"capture_hb": [300, 1800], "quota_pct": [70, 101]}}
        EVENTS = {"ts": 5, "events": [{"ts": 50 - i, "kind": "fire", "arm": "mc-a"}
                                      for i in range(5)]}
        WEEK = {"ts": 7, "days": [{"date": "2026-10-27", "realized": 0.0, "closed": 0,
                                   "by_arm": {}}]}
        posts: List[dict] = []

        class Stub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/api/health":
                    return self._send(200, HEALTH)
                if p == "/api/events":
                    return self._send(200, EVENTS)
                if p == "/api/week":
                    return self._send(200, WEEK)
                return self._send(404, {"error": "unknown endpoint"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
                except Exception:
                    body = {}
                posts.append({"path": self.path, "ctl": self.headers.get("X-Desk-Control"),
                              "body": body})
                if self.headers.get("X-Desk-Control") != "1":
                    return self._send(403, {"error": "missing control header"})
                m = _re.match(r"^/api/strategy/([A-Za-z0-9_.-]+)/(pause|resume)$", self.path)
                if m:
                    name, verb = m.group(1), m.group(2)
                    if name == "busy":
                        return self._send(429, {"error": "debounced — a control action just ran",
                                                "retry_after_s": 2})
                    if name == "mc-live" and verb == "resume" \
                            and body.get("confirm_name") != "mc-live":
                        return self._send(409, {"error": "row is live-routed — resume needs "
                                                         "typed confirmation",
                                                "expect": {"confirm_name": "mc-live"}})
                    return self._send(200, {"ok": True, "action": verb,
                                            "results": [{"arm": name, "unchanged": False}]})
                if self.path == "/api/stack/pause-all":
                    return self._send(200, {"ok": True, "action": "pause-all", "results": []})
                return self._send(404, {"error": "unknown endpoint"})

        stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as td:
            os.environ["DESK_DASH_URL"] = f"http://127.0.0.1:{stub.server_address[1]}"
            os.environ["DESK_DATA_DIR"] = td

            s.check(MS.desk_health() == HEALTH,
                    "desk_health = /api/health passthrough VERBATIM (trim nothing)")
            ev = MS.desk_events(limit=2)
            s.check(len(ev["events"]) == 2 and ev["events"] == EVENTS["events"][:2],
                    "desk_events clamps to limit, order preserved")
            s.check(MS.desk_week() == WEEK, "desk_week passthrough")
            st = MS.desk_strategies()
            s.check(st == {"ts": 111, "arms": HEALTH["arms"]},
                    "desk_strategies = the arms[] rows", str(st)[:120])

            ex = MS.desk_explain("quota")
            s.check(ex.get("found") is True and ex.get("key") == "quota_pct"
                    and "70" in (ex.get("threshold") or "") and ex.get("action"),
                    "explain('quota') fuzzy-hits quota_pct; threshold rendered from the LIVE "
                    "payload", str(ex)[:160])
            ex2 = MS.desk_explain("mode chips")
            s.check(ex2.get("found") is True and ex2.get("kind") == "concept"
                    and "OFF" in ex2.get("label", ""),
                    "explain matches concept terms (mode chips)", str(ex2.get("label")))
            ex3 = MS.desk_explain("zzqx-no-such-term")
            s.check(ex3.get("found") is False and ex3.get("available"),
                    "explain miss lists the available vocabulary")

            with open(os.path.join(td, "notifier_state.json"), "w") as f:
                json.dump({"active": {"driver_stale": {"since": 1700000000,
                                                       "msg": "Driver heartbeat 2h old"}},
                           "health_fails": 1, "book_stall": 0, "book_rows": 5}, f)
            al = MS.desk_alerts()
            s.check(al["active"] and al["active"][0]["condition"] == "driver_stale"
                    and al["active"][0]["since"] == 1700000000
                    and al["active"][0]["message"] == "Driver heartbeat 2h old"
                    and al["health_fails_in_a_row"] == 1,
                    "desk_alerts parses active conditions + transitions from the state file",
                    str(al)[:160])

            r = MS.desk_pause("mc-a")
            s.check(r.get("status") == 200 and r["body"].get("ok") is True
                    and posts[-1]["ctl"] == "1",
                    "desk_pause → audited HTTP path, X-Desk-Control sent", str(posts[-1]))
            r = MS.desk_resume("mc-live")
            s.check(r.get("status") == 409
                    and r["body"].get("expect", {}).get("confirm_name") == "mc-live",
                    "409 typed-confirm body surfaced VERBATIM", str(r)[:160])
            r = MS.desk_resume("mc-live", confirm_name="mc-live")
            s.check(r.get("status") == 200
                    and posts[-1]["body"] == {"confirm_name": "mc-live"},
                    "resume retry carries the typed confirm_name")
            r = MS.desk_pause("busy")
            s.check(r.get("status") == 429 and r["body"].get("retry_after_s") == 2,
                    "429 debounce body surfaced VERBATIM")
            r = MS.desk_pause_all()
            s.check(r.get("status") == 200
                    and posts[-1]["path"] == "/api/stack/pause-all",
                    "desk_pause_all posts the stack route")

            # dead dashboard: an error dict, never a raise into the MCP transport
            os.environ["DESK_DASH_URL"] = "http://127.0.0.1:9"
            dead = MS.desk_health()
            s.check(isinstance(dead, dict) and "error" in dead,
                    "dead dashboard → error dict (no raise)", str(dead)[:120])

        # ── docs: docs/mcp.md names every tool + the wiring line + the exclusions ──
        docp = os.path.join(REPO_ROOT, "docs", "mcp.md")
        s.check(os.path.exists(docp), "docs/mcp.md exists")
        if os.path.exists(docp):
            with open(docp) as f:
                doc = f.read()
            s.check(all(t in doc for t in EXPECTED), "docs/mcp.md names every tool",
                    str([t for t in EXPECTED if t not in doc]))
            s.check("claude mcp add predictiondesk -- python3 -m desk.mcp_server" in doc,
                    "docs/mcp.md carries the exact claude-mcp-add line")
            low = doc.lower()
            s.check("no live-arming" in low and "no order placement" in low
                    and "config or seal mutation" in low,
                    "docs/mcp.md states the exclusions")
        with open(os.path.join(REPO_ROOT, "requirements.txt")) as f:
            reqs = f.read()
        s.check(bool(_re.search(r"^mcp==\d", reqs, _re.M)), "mcp SDK pinned in requirements.txt")
    except Exception as e:
        s.check(False, "mcp_server guard ran", repr(e)[:200])
    finally:
        if stub is not None:
            try:
                stub.shutdown()
                stub.server_close()
            except Exception:
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return s


def check_verify_cli() -> Section:
    """HARD guard (harness Phase 0): the agent-facing verification CLI (desk/verify.py) — the one
    read-only surface (gate/tenants/tick/health/feature-map) agents verify the desk through. Static:
    framework-only imports (no tenant/strategies import), no config-writing identifiers, no HTTP
    POST (health is a bare GET; no X-Desk-Control anywhere), docstring states the exclusions.
    Behavior (child processes, offline): --json = exactly ONE JSON object on stdout; tenants shape +
    seal statuses; health passthrough VERBATIM off a stub dashboard and an error dict + nonzero exit
    off a dead one; tick --dry-run previews without writing a byte; a real tick on an EMPTY capture
    store is REFUSED exit 3 (never fake success); the gate wrapper's command + printed-table parser
    (canned transcript — the gate cannot run inside itself: run_eval holds a singleton lock); and
    docs/feature-map.md freshness (generated, drift fails HERE) + the written-down conventions
    (AGENTS.md pre-merge line, openspec eval-evidence rules). Desk-only safe."""
    import ast
    import http.server
    import subprocess
    import tempfile
    import threading
    s = Section("verify_cli", hard=True)
    stub = None
    try:
        import desk.verify as DV

        # ── static: framework-only, read-only ─────────────────────────────────
        with open(DV.__file__.replace(".pyc", ".py")) as f:
            src = f.read()
        tree = ast.parse(src)
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or "")
        roots = {m.split(".")[0] for m in mods}
        s.check("strategies" not in roots and "requests" not in roots,
                "no tenant package / third-party HTTP import", str(sorted(roots)))
        desk_mods = {m for m in mods if m.split(".")[0] == "desk"}
        s.check(desk_mods <= {"desk.runtime.paths", "desk.signals"},
                "desk imports = paths + registry ONLY (driver/eval run as child processes)",
                str(sorted(desk_mods)))
        idents = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
            | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        forbidden = {"flip_enabled", "pause_all", "append_audit", "seed_audit",
                     "_write_config_bytes", "seal_hash", "Request"}
        s.check(not (idents & forbidden),
                "no config-writing identifier, no urllib.Request (GET-only urlopen)",
                str(sorted(idents & forbidden)))
        s.check("X-Desk-Control" not in src, "no control header anywhere (never a control POST)")
        low = " ".join(src.lower().split())        # whitespace-normalized: phrases may wrap lines
        s.check("no order placement" in low and "no config or seal mutation" in low,
                "module docstring states the exclusions")

        # ── gate wrapper: command + parser (canned — never the gate inside the gate) ──
        cmd = DV._gate_cmd()
        s.check(cmd[0] == sys.executable and cmd[-3:] == ["-m", "desk.eval.run_eval", "--fast"],
                "gate subcommand wraps THE gate command", str(cmd))
        canned = ("=" * 60 + "\n"
                  "[PASS] fees: 9 passed, 0 failed\n"
                  "[WARN] backtest (non-gating): 0 passed, 1 failed\n"
                  "    - no captured data -> backtest smoke skipped\n"
                  "[PASS] tenantx:rolling_calib: 5 passed, 0 failed\n"
                  "[FAIL] engine: 1 passed, 2 failed\n"
                  + "=" * 60 + "\nRESULT: FAIL\n")
        parsed = DV._parse_gate_output(canned)
        names = [x["name"] for x in parsed["sections"]]
        s.check(parsed["result"] == "FAIL" and len(parsed["sections"]) == 4,
                "parser reads RESULT + every section line", str(parsed)[:160])
        s.check(names == ["fees", "backtest", "tenantx:rolling_calib", "engine"],
                "parser keeps order and handles tenant `<tenant>:<section>` names", str(names))
        s.check(parsed["sections"][1]["gating"] is False and parsed["sections"][3]["failed"] == 2,
                "parser carries non-gating flag + counts")

        # ── behavior: child processes, --json one-object contract ─────────────
        py = sys.executable
        env = dict(os.environ)

        p = subprocess.run([py, "-m", "desk.verify", "tenants", "--json"],
                           capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=120)
        s.check(p.returncode == 0, "tenants --json exits 0", (p.stderr or "")[-200:])
        tj = json.loads(p.stdout)   # whole stdout = ONE object, or this raises
        s.check({"ok", "bases", "tenants", "enabled", "rows"} <= set(tj),
                "tenants payload shape (ok/bases/tenants/enabled/rows)", str(sorted(tj))[:160])
        s.check("calendar" in tj["bases"], "builtin base listed", str(tj["bases"]))
        s.check(all({"name", "strategy", "enabled", "seal"} <= set(r) for r in tj["rows"]),
                "every config row carries a seal status")

        HEALTH = {"ts": 42, "procs": {"up": True}, "arms": [{"name": "a", "mode": "PAPER",
                                                             "enabled": True}]}

        class Stub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps(HEALTH).encode()
                self.send_response(200 if self.path == "/api/health" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        env_up = {**env, "DESK_DASH_URL": f"http://127.0.0.1:{stub.server_address[1]}"}
        p = subprocess.run([py, "-m", "desk.verify", "health", "--json"],
                           capture_output=True, text=True, cwd=REPO_ROOT, env=env_up, timeout=120)
        s.check(p.returncode == 0 and json.loads(p.stdout) == HEALTH,
                "health --json = /api/health passthrough VERBATIM, exit 0", p.stdout[:120])
        env_dead = {**env, "DESK_DASH_URL": "http://127.0.0.1:9"}
        p = subprocess.run([py, "-m", "desk.verify", "health", "--json"],
                           capture_output=True, text=True, cwd=REPO_ROOT, env=env_dead, timeout=120)
        s.check(p.returncode != 0 and "error" in json.loads(p.stdout),
                "dead dashboard -> error object + nonzero exit (no traceback)", p.stdout[:120])

        with tempfile.TemporaryDirectory() as td:
            env_td = {**env, "DESK_DATA_DIR": td}
            p = subprocess.run([py, "-m", "desk.verify", "tick", "--dry-run", "--json"],
                               capture_output=True, text=True, cwd=REPO_ROOT, env=env_td,
                               timeout=180)
            dj = json.loads(p.stdout)
            s.check(p.returncode == 0 and dj.get("dry_run") is True and "would_fire" in dj,
                    "tick --dry-run exits 0 and reports what WOULD fire", p.stdout[:160])
            s.check(dj.get("store", {}).get("books_present") is False,
                    "dry-run states the empty store", str(dj.get("store")))
            s.check(os.listdir(td) == [], "dry-run writes NOTHING under the data root",
                    str(os.listdir(td)))
            p = subprocess.run([py, "-m", "desk.verify", "tick", "--json"],
                               capture_output=True, text=True, cwd=REPO_ROOT, env=env_td,
                               timeout=180)
            rj = json.loads(p.stdout)
            s.check(p.returncode == 3 and rj.get("refused") is True,
                    "EMPTY store: real tick REFUSED exit 3 (never fake success)",
                    f"rc={p.returncode} {p.stdout[:120]}")
            s.check("book snapshots" in rj.get("error", ""), "refusal names the missing data")
            s.check(os.listdir(td) == [], "refused tick writes NOTHING (driver never launched)",
                    str(os.listdir(td)))

        # ── feature map: generated, deterministic, fresh ───────────────────────
        s.check(DV.render_feature_map() == DV.render_feature_map(),
                "feature-map output deterministic (no timestamps)")
        status, detail = DV.feature_map_status()
        s.check(status in ("fresh", "core-fresh"),
                "docs/feature-map.md fresh (regenerate: python3 -m desk.verify feature-map --write)",
                f"{status}: {detail}")
        p = subprocess.run([py, "-m", "desk.verify", "feature-map", "--check", "--json"],
                           capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=120)
        s.check(p.returncode == 0 and json.loads(p.stdout).get("ok") is True,
                "feature-map --check green via the CLI", p.stdout[:120])

        # ── conventions written down (AGENTS.md + openspec config) ────────────
        with open(os.path.join(REPO_ROOT, "AGENTS.md")) as f:
            agents = f.read()
        s.check("docs/feature-map.md" in agents and "python3 -m desk.verify" in agents,
                "AGENTS.md names the verify CLI + feature map")
        s.check("desk verify gate" in agents and "merge" in agents.lower(),
                "AGENTS.md carries the pre-merge convention (gate green before merge)")
        ocfg = os.path.join(REPO_ROOT, "openspec", "config.yaml")
        if os.path.exists(ocfg):
            with open(ocfg) as f:
                oc = f.read()
            s.check("eval section" in oc and "eval evidence" in oc,
                    "openspec rules: proposals name eval sections; tasks need eval evidence")
        else:
            s.notes.append("openspec/config.yaml absent (public cut?) — rules check skipped")
    except Exception as e:
        s.check(False, "verify_cli guard ran", repr(e)[:200])
    finally:
        if stub is not None:
            try:
                stub.shutdown()
                stub.server_close()
            except Exception:
                pass
    return s


def main() -> int:
    # The GATE runs with the taker-fees knob pinned ON for the whole process: every section verifies
    # FORMULA/engine behavior, not the deployment choice (DESK_TAKER_FEES_ENABLED is a runtime lever
    # for daemons; it must never be able to turn the golden green/red).
    os.environ["DESK_TAKER_FEES_ENABLED"] = "true"
    # SINGLETON: concurrent run_eval instances stampede the capture store. Unlike daemon ticks (skip = fine),
    # a gate run must FAIL LOUD, never silently skip or fake-pass.
    import fcntl
    _lk = open("/tmp/desk_runeval.lock", "w")
    try:
        fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("FAIL: another run_eval is already running (singleton lock /tmp/desk_runeval.lock) — "
              "wait for it or kill it; refusing to stampede the store")
        return 2
    fast = "--fast" in sys.argv       # --fast: every HARD section (store-free, seconds); the store/network
    with open(GOLDEN) as f:           # smokes (backtest/recon) run in the full gate only.
        g = json.load(f)
    sections = [check_fees(g), check_engine(g), check_sample(), check_wide_book(), check_run_dir(),
                check_registry(), check_discovery_hook(), check_recon_lifecycle(), check_compaction_recount(),
                check_compactor_safety(),
                check_settlement_chain(), check_exit_rules(), check_driver_reload(),
                check_exitmon_sweep(), check_control_api(), check_notifier(), check_daily_cap(),
                check_supervisor(), check_ops_page(), check_mcp_server(), check_verify_cli()]
    if not fast:
        sections += [check_backtest(), check_recon()]
    sections += tenant_sections()   # plug-in gates last, labeled <tenant>:<section>
    print("=" * 60)
    hard_fail = False
    for sec in sections:
        status = "PASS" if sec.failed == 0 else ("FAIL" if sec.hard else "WARN")
        tag = "" if sec.hard else " (non-gating)"
        print(f"[{status}] {sec.name}{tag}: {sec.passed} passed, {sec.failed} failed")
        for n in sec.notes:
            print(f"    - {n}")
        if sec.failed and sec.hard:
            hard_fail = True
    print("=" * 60)
    print("RESULT:", "FAIL" if hard_fail else "PASS")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
