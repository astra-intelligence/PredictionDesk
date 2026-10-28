"""signals/registry.py — the strategy registry the driver loops over, with PLUGIN DISCOVERY.

Specs come from two places:
  • BUILTINS — framework samples shipped in desk/signals (the no-edge `calendar` smoke strategy).
  • TENANTS  — plug-in packages discovered under the strategies dir (env `DESK_STRATEGIES_DIR`,
    default `<repo>/strategies`). A tenant is any subdirectory carrying a `manifest.json`:

        {"name": "<spec name>", "module": "<top-level package>", "version": "<behavior version>",
         "entrypoint": "pkg.mod:generate_orders", "schedule": {...}, "enabled": false,
         "params": {...default params...},
         "hooks": {"eval_checks": "pkg.eval_checks:sections", "resolvers": "pkg...:fn",
                   "exit_order_builder": "pkg...:fn", "event_slug_of": "pkg...:fn",
                   "discover_event_slugs": "pkg...:fn"},
         "bootstrap": ["pkg.mod", ...], "playbook": "relative/path.md"}

    (`date_discovery_cities` is the DEPRECATED alias of `discover_event_slugs` — the framework
    wraps the city list through the legacy temperature template; see capture_select. Dashboard
    adapters are NOT a registry hook: they live with the desk-dashboard skill.)

    The strategies dir goes on sys.path (ensure_importable), so the tenant package imports under its
    own top-level name and all its internal imports stay package-relative. A malformed manifest, a
    missing tenant, or a failing entrypoint is LOUDLY skipped — never silent: the config rows that
    reference it are refused by name, so nothing trades on a half-loaded plugin.

Config (strategy_config.json, overridable with env `DESK_STRATEGY_CONFIG`) overlays
enabled/schedule/params onto the discovered defaults so a sweep's winning params can be written to
config and picked up live without code edits.

VARIANT specs: a config row whose `name` is NOT a discovered spec but carries `"strategy": "<base>"`
becomes its own spec (own name = strategy_id, own params) sharing the base's generate_orders — so
several parameterizations of one strategy can run side by side. PRE-REGISTRATION: a row may carry
`"config_hash"`; the loader recomputes the seal and REFUSES to enable the spec on mismatch
(fail-closed: silent param drift can never masquerade as the registered strategy). See `seal_hash`
for what the seal covers; reseal flow documented in AGENTS.md.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from desk.types import StrategySpec, StrategyConfig

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "strategy_config.json")
CONFIG_PATH = DEFAULT_CONFIG_PATH          # back-compat alias; prefer config_path()
# this file lives at <repo>/desk/signals/registry.py
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Version string per BUILTIN spec — folded into the seal exactly like a tenant manifest's "version"
# (bump it whenever a builtin's default params / behavior change, then reseal the affected rows).
BUILTIN_VERSIONS = {"calendar": "1"}


def config_path() -> str:
    """The strategy-config path: env DESK_STRATEGY_CONFIG when set, else the shipped default."""
    return os.environ.get("DESK_STRATEGY_CONFIG") or DEFAULT_CONFIG_PATH


def strategies_dir() -> str:
    """The tenant plug-in root: env DESK_STRATEGIES_DIR when set, else <repo>/strategies."""
    return os.environ.get("DESK_STRATEGIES_DIR") or os.path.join(REPO_ROOT, "strategies")


def ensure_importable(d: Optional[str] = None) -> str:
    """Put the strategies dir on sys.path (idempotent) so tenant packages import under their own
    top-level names. Returns the dir."""
    d = os.path.abspath(d or strategies_dir())
    if os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)
    return d


def discover_tenants() -> List[dict]:
    """Tenant manifests under strategies_dir(), sorted by directory name. Malformed or incomplete
    manifests are LOUDLY skipped. Returns [] (quietly) when the dir itself is absent — that is the
    legitimate desk-only tree, not an error."""
    d = strategies_dir()
    out: List[dict] = []
    if not os.path.isdir(d):
        return out
    for sub in sorted(os.listdir(d)):
        mf = os.path.join(d, sub, "manifest.json")
        if not os.path.isfile(mf):
            continue                                   # plain dir, not a tenant
        try:
            with open(mf) as f:
                m = json.load(f)
        except Exception as e:
            print(f"[registry] SKIPPING tenant dir '{sub}': malformed manifest.json ({e!r})", flush=True)
            continue
        missing = [k for k in ("name", "module", "entrypoint") if not m.get(k)]
        if missing:
            print(f"[registry] SKIPPING tenant dir '{sub}': manifest.json missing {missing}", flush=True)
            continue
        m["_dir"] = os.path.join(d, sub)
        out.append(m)
    return out


def load_entrypoint(spec: str):
    """Resolve 'pkg.mod:attr' to the attr (or 'pkg.mod' to the module), with the strategies dir on
    sys.path. Raises on failure — callers decide whether that is loud-skip or fatal."""
    ensure_importable()
    mod_name, _, attr = spec.partition(":")
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr) if attr else mod


def _builtin_specs() -> Dict[str, StrategySpec]:
    # Lazy import so registry import stays cheap and avoids cycles.
    from desk.signals.example_calendar import generate_orders as calendar_go
    return {
        # Sample strategy shipped with the framework: a no-edge calendar smoke test that exercises
        # the full ladder-walk / maker-fill / fee path end-to-end. Disabled by default; enable it
        # (or add your own tenant) via strategy_config.json.
        "calendar": StrategySpec(
            name="calendar",
            schedule={"type": "interval", "secs": 21600},
            enabled=False, params={"qty": 50.0, "n_markets": 3, "mode": "taker"},
            generate_orders=calendar_go),
    }


_TENANT_OF: Dict[str, dict] = {}     # base spec name -> manifest (refreshed by base_specs)
_SPEC_TENANT: Dict[str, dict] = {}   # EVERY loaded spec name (incl. variants) -> manifest (load_registry)


def base_specs() -> Dict[str, StrategySpec]:
    """Builtin + tenant-manifest specs (the bases config rows overlay). A tenant whose entrypoint
    fails to import is LOUDLY skipped, so its config rows are later refused by name."""
    specs = _builtin_specs()
    _TENANT_OF.clear()
    for m in discover_tenants():
        name = m["name"]
        if name in specs:
            print(f"[registry] SKIPPING tenant '{name}' ({m['_dir']}): name collides with an "
                  f"already-registered spec", flush=True)
            continue
        try:
            go = load_entrypoint(m["entrypoint"])
        except Exception as e:
            print(f"[registry] SKIPPING tenant '{name}': entrypoint {m['entrypoint']!r} failed to "
                  f"import ({e!r}) — config rows naming it will be refused", flush=True)
            continue
        specs[name] = StrategySpec(name=name, schedule=dict(m.get("schedule") or {}),
                                   enabled=bool(m.get("enabled", False)),
                                   params=dict(m.get("params") or {}), generate_orders=go)
        _TENANT_OF[name] = m
    return specs


def tenant_for(spec_name: str) -> Optional[dict]:
    """The manifest behind a loaded spec/variant name (None for builtins/unknown). Valid after
    load_registry (variants) or base_specs (bases)."""
    return _SPEC_TENANT.get(spec_name) or _TENANT_OF.get(spec_name)


def hook_for(spec_name: str, hook: str) -> Optional[Callable]:
    """The tenant hook behind a loaded spec name, or None (builtin / tenant absent / hook not
    declared). A declared hook that fails to import is LOUD, then None (fail-soft for callers)."""
    m = tenant_for(spec_name)
    if not m:
        return None
    target = (m.get("hooks") or {}).get(hook)
    if not target:
        return None
    try:
        return load_entrypoint(target)
    except Exception as e:
        print(f"[registry] tenant '{m['name']}' hook '{hook}' = {target!r} failed to import ({e!r})",
              flush=True)
        return None


def hooks_for(hook: str) -> List[Tuple[str, Callable]]:
    """(tenant_name, fn) for every discovered tenant declaring `hook`. Import failures are LOUD and
    the tenant is skipped for that hook."""
    out: List[Tuple[str, Callable]] = []
    for m in discover_tenants():
        target = (m.get("hooks") or {}).get(hook)
        if not target:
            continue
        try:
            out.append((m["name"], load_entrypoint(target)))
        except Exception as e:
            print(f"[registry] tenant '{m['name']}' hook '{hook}' = {target!r} failed to import "
                  f"({e!r}) — skipped", flush=True)
    return out


_MARKET_RESOLVERS: Optional[tuple] = None


def market_resolvers(refresh: bool = False) -> tuple:
    """Composed (peer_key, category_of) across every tenant's `resolvers` hook (each hook returns the
    pair, fail-soft (None, None) when its metadata is unavailable). (None, None) when no tenant
    contributes — callers then use the single default market class. With resolvers present, an
    unclassified market resolves to 'other', which the live executor REFUSES under a scoped PASS
    (fail-closed). Memoized per process — the driver/exit monitor resolve once, not per fire."""
    global _MARKET_RESOLVERS
    if _MARKET_RESOLVERS is not None and not refresh:
        return _MARKET_RESOLVERS
    pairs = []
    for tname, fn in hooks_for("resolvers"):
        try:
            pk, co = fn()
        except Exception as e:
            print(f"[registry] tenant '{tname}' resolvers hook failed ({e!r}) — skipped", flush=True)
            continue
        if pk is not None or co is not None:
            pairs.append((pk, co))
    if not pairs:
        _MARKET_RESOLVERS = (None, None)
    elif len(pairs) == 1:
        _MARKET_RESOLVERS = pairs[0]
    else:
        def _peer_key(m):
            for pk, _ in pairs:
                if pk is not None:
                    v = pk(m)
                    if v is not None:
                        return v
            return None

        def _category_of(m):
            for _, co in pairs:
                if co is not None:
                    v = co(m)
                    if v not in (None, "other", "unknown"):
                        return v
            return "other"
        _MARKET_RESOLVERS = (_peer_key, _category_of)
    return _MARKET_RESOLVERS


def playbook_for(spec_name: str) -> Optional[str]:
    """Absolute path of the tenant playbook behind a spec/variant name, or None (builtin / no tenant /
    none declared). Resolves variant names to their base through the active config."""
    m = tenant_for(spec_name)
    if m is None:
        try:
            with open(config_path()) as f:
                rows = json.load(f).get("strategies", [])
        except Exception:
            rows = []
        base = next((r.get("strategy", r.get("name")) for r in rows if r.get("name") == spec_name),
                    spec_name)
        m = next((t for t in discover_tenants() if t["name"] == base), None)
    if not m or not m.get("playbook"):
        return None
    p = os.path.join(m["_dir"], m["playbook"])
    return p if os.path.exists(p) else None


def config_hash(row: dict) -> str:
    """LEGACY row-only hash: sha256 over {name, strategy, schedule, params} JSON with sorted keys.
    Superseded by seal_hash (which also covers the base's defaults + version — the row-only hash left
    base-default edits invisible to the seal); kept only to identify old-format seals in history."""
    canon = {k: row.get(k) for k in ("name", "strategy", "schedule", "params")}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def base_version(base_name: str) -> str:
    """The base's behavior-version string folded into every dependent row's seal: a tenant manifest's
    "version" (or the BUILTIN_VERSIONS entry). Call after base_specs() so tenant manifests are fresh."""
    m = _TENANT_OF.get(base_name)
    if m is not None:
        return str(m.get("version", "0"))
    return str(BUILTIN_VERSIONS.get(base_name, "0"))


def seal_hash(row: dict, base: StrategySpec, base_ver: Optional[str] = None) -> str:
    """The row SEAL: sha256 over the row's EFFECTIVE behavior, JSON with sorted keys —
      {name, strategy, schedule: row's-or-base's, params: {**base defaults, **row params},
       base_version: the base's version string}.
    This closes the defaults-under-the-hash hole (A5.1): the loader merges base defaults UNDER the
    row's params, so a base-default edit (or a tenant version bump) changes effective behavior — with
    the EFFECTIVE merge hashed, that edit breaks every dependent row's seal until the operator
    explicitly reseals (`python3 -m desk.signals.registry reseal --write`). The seal still means
    "this exact behavior was registered", now including what the base contributes."""
    eff_params = {**(base.params or {}), **(row.get("params") or {})}
    canon = {"name": row.get("name"),
             "strategy": row.get("strategy", row.get("name")),
             "schedule": row.get("schedule") or base.schedule or {},
             "params": eff_params,
             "base_version": base_ver if base_ver is not None else base_version(row.get("strategy", row.get("name")))}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal_hash_for(row: dict, bases: Optional[Dict[str, StrategySpec]] = None) -> Optional[str]:
    """seal_hash with the base resolved from discovery; None when the base is unavailable (then the
    seal is UNVERIFIABLE and the loader refuses the row — a missing tenant, not a tamper)."""
    bases = bases if bases is not None else base_specs()
    base = bases.get(row.get("strategy", row.get("name")))
    return None if base is None else seal_hash(row, base)


def load_registry(config_path_arg: Optional[str] = None,
                  include_disabled: bool = False) -> List[StrategySpec]:
    """Discovered specs overlaid with config (+ variant specs); returns ENABLED, hash-verified specs
    only. Every refusal/skip is LOUD. include_disabled=True additionally returns DISABLED (but still
    seal-verified) specs — the exit monitor resolves exit params for PAUSED arms through this, because
    `enabled: false` means "no new entries", never "no risk management". REFUSED rows (tampered /
    unsealed / absent base) are never returned either way."""
    specs = base_specs()
    path = config_path_arg or config_path()
    rows: List[dict] = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                rows = json.load(f).get("strategies", [])
        except Exception as e:
            print(f"[registry] REFUSING config {path}: malformed JSON ({e!r}) — NO overlay rows "
                  f"loaded (defaults only, all disabled)", flush=True)
            rows = []
    elif config_path_arg or os.environ.get("DESK_STRATEGY_CONFIG"):
        print(f"[registry] config {path} does not exist — no overlay rows", flush=True)
    _SPEC_TENANT.clear()
    for row in rows:
        sc = StrategyConfig.from_json(row)
        base_name = row.get("strategy", sc.name)
        base = specs.get(base_name)
        if base is None:
            # LOUD, fail-closed: an absent tenant (or typo'd base) must never silently drop a row —
            # and a same-name builtin fallback must not trade in its place either.
            print(f"[registry] SKIPPING row '{sc.name}': strategy base '{base_name}' is not "
                  f"available (no builtin, no tenant under {strategies_dir()}) — row disabled",
                  flush=True)
            specs.pop(sc.name, None)
            continue
        declared = row.get("config_hash")
        expected = seal_hash(row, base, base_version(base_name))
        if declared and declared != expected:
            print(f"[registry] REFUSING '{sc.name}': config_hash mismatch (declared {declared[:12]}… "
                  f"expected {expected[:12]}…) — pre-registration violated OR the base's "
                  f"defaults/version drifted under the row; review, then reseal "
                  f"(python3 -m desk.signals.registry reseal --write). Spec disabled.", flush=True)
            # FAIL-CLOSED: a refused overlay must also kill the SAME-NAME base — otherwise a tampered
            # row silently falls back to the base's default config and the sealed name still trades.
            specs.pop(sc.name, None)
            continue
        if not declared:
            # unsealed rows skip verification entirely — the same fail-open class. Refuse + kill base.
            print(f"[registry] REFUSING '{sc.name}': row has NO config_hash — unsealed rows cannot load "
                  f"(seal it: python3 -m desk.signals.registry reseal --write)", flush=True)
            specs.pop(sc.name, None)
            continue
        sealed = declared   # the seal — the one identity live TradeRows must carry
        if sc.name in specs:                       # overlay onto the base itself
            spec = specs[sc.name]
            spec.enabled = sc.enabled
            if sc.schedule:
                spec.schedule = sc.schedule
            spec.params = {**spec.params, **(sc.params or {}), "_config_hash": sealed}
        else:                                      # VARIANT: own spec, shared generate_orders
            spec = StrategySpec(
                name=sc.name, schedule=(sc.schedule or base.schedule), enabled=sc.enabled,
                params={**base.params, **(sc.params or {}), "strategy_id": sc.name,
                        "_config_hash": sealed},
                generate_orders=base.generate_orders)
            specs[sc.name] = spec
        if base_name in _TENANT_OF:
            _SPEC_TENANT[sc.name] = _TENANT_OF[base_name]
    return [s for s in specs.values() if s.enabled or include_disabled]


def _cli(argv: List[str]) -> int:
    """`python3 -m desk.signals.registry <cmd>`:
      list      — discovered bases + loaded (enabled) specs
      reseal    — recompute every config row's seal; --write rewrites the config file in place
      bootstrap — run each tenant's manifest `bootstrap` modules (as __main__), in manifest order
    """
    cmd = argv[0] if argv else "list"
    if cmd == "bootstrap":
        tenants = discover_tenants()
        if not tenants:
            print(f"[bootstrap] no tenants under {strategies_dir()} — nothing to bootstrap")
            return 0
        ensure_importable()
        import runpy
        import sys as _sys
        saved_argv = _sys.argv
        try:
            for m in tenants:
                mods = m.get("bootstrap") or []
                if not mods:
                    print(f"[bootstrap] {m['name']}: no bootstrap modules declared — skipped")
                    continue
                for mod in mods:
                    print(f"[bootstrap] {m['name']}: python -m {mod}", flush=True)
                    # runpy inherits THIS process's sys.argv — a bootstrap module parsing its own CLI
                    # (e.g. calibrate's optional end-date positional) would read "bootstrap" as an
                    # argument and crash. Each module runs with a clean argv, like a real `python -m`.
                    _sys.argv = [mod]
                    runpy.run_module(mod, run_name="__main__")
        finally:
            _sys.argv = saved_argv
        return 0
    if cmd == "list":
        bases = base_specs()
        print(json.dumps({"strategies_dir": strategies_dir(), "config": config_path(),
                          "bases": sorted(bases), "tenants": sorted(_TENANT_OF),
                          "enabled": [s.name for s in load_registry()]}, indent=2))
        return 0
    if cmd == "reseal":
        write = "--write" in argv
        path = config_path()
        with open(path) as f:
            cfg = json.load(f)
        bases = base_specs()
        changed = 0
        for row in cfg.get("strategies", []):
            base = bases.get(row.get("strategy", row.get("name")))
            if base is None:
                print(f"  {row.get('name')}: base missing — cannot seal (tenant not discovered)")
                continue
            new = seal_hash(row, base)
            old = row.get("config_hash")
            if old != new:
                changed += 1
                print(f"  {row.get('name')}: {str(old)[:12]}… -> {new[:12]}…")
                row["config_hash"] = new
            else:
                print(f"  {row.get('name')}: seal unchanged ({new[:12]}…)")
        if write and changed:
            with open(path, "w") as f:
                json.dump(cfg, f, indent=1)
                f.write("\n")
            print(f"resealed {changed} row(s) -> {path}")
        elif changed:
            print(f"{changed} row(s) need resealing — rerun with --write")
        return 0
    print(f"unknown command {cmd!r} — use: list | reseal [--write]")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
