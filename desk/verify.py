"""desk/verify.py — the agent-facing verification CLI (harness Phase 0).

One read-only surface that answers "is the desk okay, and how do I prove it":

    gate         run the offline golden gate (desk.eval.run_eval --fast); exit code mirrors it
    tenants      registry summary: dirs, bases, tenants, enabled specs, per-row seal status
    tick         one paper driver tick (desk.driver.strategy_driver --once); --dry-run previews
                 what would fire without firing; REFUSES loudly on an empty capture store
    health       read-only GET of the dashboard's /api/health (never a POST)
    feature-map  generate docs/feature-map.md (feature -> files -> eval section -> verify command)
                 from the eval gate's sections + the tenant registry; --write / --check

`--json` on any subcommand prints exactly ONE JSON object on stdout (diagnostics go to stderr) so
agents parse instead of scraping. Exit codes: 0 ok, 1 check failed, 2 usage, 3 refused preflight.

WHAT THIS MODULE DELIBERATELY EXCLUDES (by design, not omission): no order placement, no config or
seal mutation, no control POSTs (pause/resume is the dashboard / MCP server's audited job) — its
only HTTP use is the health GET. The eval gate's `verify_cli` section statically enforces this.
`gate` and `tick` delegate to the real entrypoints as CHILD PROCESSES so singleton locks, env
pinning and signal handling stay the one implementation's — never a parallel reimplementation.

Run:  python3 -m desk.verify <subcommand> [--json]
Env:  DESK_DASH_URL (default http://127.0.0.1:$DASH_PORT, DASH_PORT default 8788),
      DESK_DATA_DIR (re-roots the capture store tick preflights), DESK_STRATEGIES_DIR,
      DESK_STRATEGY_CONFIG (registry resolution, as everywhere).
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional, Tuple

from desk.runtime.paths import REPO_ROOT, data_path, repo_path


# ── gate ───────────────────────────────────────────────────────────────────────
def _gate_cmd() -> List[str]:
    """The ONE gate command this CLI wraps — eval-fast, the PR gate, in a child process (the gate
    owns its singleton lock + env pinning; running it in-process would also deadlock gate-in-gate)."""
    return [sys.executable, "-m", "desk.eval.run_eval", "--fast"]


# matches run_eval's per-section line: `[PASS] name: 3 passed, 0 failed` — name may itself contain
# colons (tenant sections are `<tenant>:<section>`), so the tail is anchored, the name non-greedy.
_SECTION_LINE = re.compile(r"^\[(PASS|FAIL|WARN)\] (.+?)( \(non-gating\))?: (\d+) passed, (\d+) failed$")


def _parse_gate_output(text: str) -> dict:
    """Parse run_eval's printed table into {result, sections:[{name,status,gating,passed,failed}]}."""
    sections: List[dict] = []
    result = None
    for raw in text.splitlines():
        m = _SECTION_LINE.match(raw.strip())
        if m:
            sections.append({"name": m.group(2), "status": m.group(1), "gating": m.group(3) is None,
                             "passed": int(m.group(4)), "failed": int(m.group(5))})
        elif raw.startswith("RESULT:"):
            result = raw.split(":", 1)[1].strip()
    return {"result": result, "sections": sections}


def cmd_gate(as_json: bool) -> int:
    cmd = _gate_cmd()
    t0 = time.time()
    if not as_json:
        return subprocess.run(cmd, cwd=REPO_ROOT).returncode
    p = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    parsed = _parse_gate_output(p.stdout or "")
    out = {"command": " ".join(cmd), "exit_code": p.returncode,
           "result": parsed["result"] or ("PASS" if p.returncode == 0 else "FAIL"),
           "duration_s": round(time.time() - t0, 1), "sections": parsed["sections"]}
    if p.returncode != 0:
        out["failing"] = [x["name"] for x in parsed["sections"] if x["status"] == "FAIL"]
        out["stderr_tail"] = (p.stderr or "")[-2000:]
    print(json.dumps(out))
    return p.returncode


# ── tenants ────────────────────────────────────────────────────────────────────
def cmd_tenants(as_json: bool) -> int:
    from desk.signals import registry as R
    with contextlib.redirect_stdout(sys.stderr):   # registry loud-skips stay diagnostics, not payload
        bases = R.base_specs()
        tenants = R.discover_tenants()
        enabled = [s.name for s in R.load_registry()]
    with open(R.config_path()) as f:
        cfg = json.load(f)
    rows, mismatched = [], []
    for r in cfg.get("strategies", []):
        declared = r.get("config_hash")
        expected = R.seal_hash_for(r, bases)
        if not declared:
            seal = "UNSEALED (loader refuses)"
        elif expected is None:
            seal = "unverifiable (base absent — loader refuses)"
        elif expected == declared:
            seal = "ok"
        else:
            seal = "MISMATCH (loader refuses — reseal via the registry CLI if the change was intended)"
            mismatched.append(r.get("name"))
        rows.append({"name": r.get("name"), "strategy": r.get("strategy", r.get("name")),
                     "enabled": bool(r.get("enabled")), "seal": seal})
    out = {"ok": not mismatched, "strategies_dir": R.strategies_dir(), "config": R.config_path(),
           "bases": sorted(bases), "tenants": sorted(m["name"] for m in tenants),
           "enabled": enabled, "rows": rows}
    print(json.dumps(out) if as_json else json.dumps(out, indent=2))
    return 0 if not mismatched else 1


# ── tick ───────────────────────────────────────────────────────────────────────
def _capture_books_present(cap: str) -> bool:
    """True iff the capture store holds at least one book-snapshot parquet (first hit wins)."""
    if not os.path.isdir(cap):
        return False
    for root, _dirs, files in os.walk(cap):
        if os.path.basename(root) == "book_snapshots" and any(f.endswith(".parquet") for f in files):
            return True
    return False


def cmd_tick(as_json: bool, dry_run: bool) -> int:
    cap = data_path("data", "capture")
    books = _capture_books_present(cap)
    if dry_run:
        from desk.signals import registry as R
        with contextlib.redirect_stdout(sys.stderr):
            specs = R.load_registry()
        out = {"dry_run": True,
               "store": {"capture_dir": cap, "books_present": books},
               "live_gates": {"LIVE_TRADING_ENABLED": os.environ.get("LIVE_TRADING_ENABLED"),
                              "recon_pass_present":
                                  os.path.exists(repo_path("desk", "eval", "recon", "PASS.json"))},
               "would_fire": [{"name": s.name, "schedule": s.schedule,
                               "mode": (s.params or {}).get("mode"),
                               "live": bool((s.params or {}).get("live"))} for s in specs],
               "note": "driver --once fires EVERY enabled row; nothing was fired or written"}
        if not books:
            out["warning"] = f"capture store empty ({cap}) — a real tick would be REFUSED"
        print(json.dumps(out) if as_json else json.dumps(out, indent=2))
        return 0
    if not books:
        msg = (f"REFUSED: no book snapshots under {cap} — a tick would price nothing and look like "
               f"success. Fix: `bash scripts/run_capture.sh` (or point DESK_DATA_DIR at a data root "
               f"with capture data). Preview without data: `python3 -m desk.verify tick --dry-run`.")
        if as_json:
            print(json.dumps({"refused": True, "error": msg, "capture_dir": cap}))
        else:
            print(msg, file=sys.stderr)
        return 3
    cmd = [sys.executable, "-m", "desk.driver.strategy_driver", "--once"]
    if not as_json:
        return subprocess.run(cmd, cwd=REPO_ROOT).returncode
    p = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    events, errors, lock_skipped = [], [], False
    for ln in (p.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            events.append(obj)
            if any(k in obj for k in ("error", "settle_error", "reconcile_error")):
                errors.append(obj)
        elif "skipping this tick" in ln:
            lock_skipped = True                        # singleton lock held = NOT a completed tick
    code = p.returncode or (1 if errors or lock_skipped else 0)
    print(json.dumps({"ok": code == 0, "command": " ".join(cmd), "exit_code": p.returncode,
                      "events": events, "errors": errors, "skipped_lock_held": lock_skipped}))
    return code


# ── health ─────────────────────────────────────────────────────────────────────
def _dash_url() -> str:
    url = os.environ.get("DESK_DASH_URL")
    if url:
        return url.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('DASH_PORT', '8788')}"


def cmd_health(as_json: bool) -> int:
    url = _dash_url() + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:    # GET only — read-only by design
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if as_json:
            print(json.dumps({"error": f"dashboard unreachable: {e!r}", "url": url}))
        else:
            print(f"UNREACHABLE {url}: {e!r}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(payload))                            # verbatim passthrough, trim nothing
        return 0
    procs = payload.get("procs") or {}
    print(f"health @ {url}  ts={payload.get('ts')}  procs_up={procs.get('up')}")
    for a in payload.get("arms") or []:
        print(f"  {a.get('name')}: mode={a.get('mode')} enabled={a.get('enabled')}")
    return 0


# ── feature-map ────────────────────────────────────────────────────────────────
_FM_PATH = ("docs", "feature-map.md")
_TENANT_MARK = "<!-- tenants:begin -->"
_GATE_VERIFY = "python3 -m desk.verify gate"
# Fastest targeted verify per section; everything not listed is verified by the gate run itself.
# This dict is reviewed code — the DOC stays generated (never hand-edited).
_VERIFY_OVERRIDE = {
    "registry": "python3 -m desk.verify tenants",
    "driver_reload": "python3 -m desk.verify tick --dry-run",
    "exitmon_sweep": "python3 -m desk.verify tick --dry-run",
    "control_api": "python3 -m desk.verify health",
    "ops_page": "python3 -m desk.verify health",
    "notifier": "python3 -m desk.verify health",
    "mcp_server": "python3 -m desk.verify health",
    "verify_cli": "python3 -m desk.verify feature-map --check",
}
# Reference noise, not feature surface: shared vocabulary every section touches.
_FM_EXCLUDE_FILES = {"desk/eval/api.py", "desk/eval/run_eval.py", "desk/types.py"}


def _import_aliases(tree: ast.Module) -> dict:
    """run_eval's top-level import table: bound name -> desk.* dotted ref."""
    al: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "desk" and a.asname:
                    al[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] == "desk":
                for a in node.names:
                    al[a.asname or a.name] = f"{mod}.{a.name}"
    return al


def _module_file(dotted: str) -> Optional[str]:
    """Repo-relative file for a desk dotted ref ('desk.sim.fees' or 'desk.sim.engine.execute'):
    longest prefix that resolves to a module file (or package dir) in this checkout."""
    parts = dotted.split(".")
    while parts:
        cand = os.path.join(*parts) + ".py"
        if os.path.exists(repo_path(cand)):
            return "/".join(parts) + ".py"
        if os.path.exists(repo_path(os.path.join(*parts, "__init__.py"))):
            return "/".join(parts) + "/"
        parts = parts[:-1]
    return None


def _fn_desk_files(fn: ast.FunctionDef, aliases: dict) -> List[str]:
    """desk files a section function exercises: its own desk imports + used top-level aliases."""
    mods = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names if a.name.split(".")[0] == "desk")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] == "desk":
                mods.update(f"{mod}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Name) and node.id in aliases:
            mods.add(aliases[node.id])
    files = {f for f in (_module_file(m) for m in mods) if f and f not in _FM_EXCLUDE_FILES}
    return sorted(files)


def _section_of(fn: ast.FunctionDef) -> Tuple[Optional[str], bool]:
    """(section name, hard) from the Section("…", hard=…) call inside a check_* function."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Section":
            name = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
            hard = True
            for kw in node.keywords:
                if kw.arg == "hard" and isinstance(kw.value, ast.Constant):
                    hard = bool(kw.value.value)
            return name, hard
    return None, True


def _fast_checks(tree: ast.Module) -> set:
    """check_* function names in main()'s initial `sections = [...]` list (the --fast set)."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            for st in ast.walk(node):
                if isinstance(st, ast.Assign) and isinstance(st.value, ast.List) \
                        and any(isinstance(t, ast.Name) and t.id == "sections" for t in st.targets):
                    return {c.func.id for c in st.value.elts
                            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    return set()


def _core_rows() -> List[dict]:
    with open(repo_path("desk", "eval", "run_eval.py")) as f:
        tree = ast.parse(f.read())
    aliases = _import_aliases(tree)
    fast = _fast_checks(tree)
    rows = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("check_"):
            name, hard = _section_of(node)
            if not name:
                continue
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            summary = doc[0].strip().replace("|", "\\|") if doc else ""
            if len(summary) > 140:
                summary = summary[:137].rstrip() + "…"
            rows.append({"section": name, "hard": hard,
                         "gate": "fast" if node.name in fast else "full",
                         "summary": summary or name,
                         "files": _fn_desk_files(node, aliases),
                         "verify": _VERIFY_OVERRIDE.get(name, _GATE_VERIFY)})
    return rows


def _tenant_rows() -> List[dict]:
    from desk.signals import registry as R
    with contextlib.redirect_stdout(sys.stderr):
        tenants = R.discover_tenants()
    rows = []
    for m in sorted(tenants, key=lambda t: t["name"]):
        rel = os.path.relpath(m.get("_dir", ""), REPO_ROOT)
        rows.append({"name": m["name"], "version": str(m.get("version", "")),
                     "dir": rel.replace(os.sep, "/") + "/",
                     "eval_hook": (m.get("hooks") or {}).get("eval_checks") or "(none)"})
    return rows


def render_feature_map() -> str:
    """The full doc text — deterministic (no timestamps): same tree, same bytes."""
    lines = [
        "# Feature map",
        "",
        "<!-- GENERATED — do not hand-edit. Regenerate: `python3 -m desk.verify feature-map --write`.",
        "     Freshness is enforced by the eval gate's `verify_cli` section",
        "     (`python3 -m desk.verify feature-map --check`). Derived from desk/eval/run_eval.py's",
        "     section functions + the tenant registry. -->",
        "",
        "Feature → files → eval section → verify command. The eval gate is the feature inventory:",
        "every guarded behavior has a section, and `python3 -m desk.verify gate` runs every row.",
        "`Eval section` reads `name · HARD|soft · fast|full` (fast = in the `--fast` PR gate).",
        "",
        "## Core framework",
        "",
        "| Feature | Files | Eval section | Verify |",
        "|---|---|---|---|",
    ]
    for r in _core_rows():
        files = ", ".join(f"`{f}`" for f in r["files"]) or "—"
        sec = f"`{r['section']}` · {'HARD' if r['hard'] else 'soft'} · {r['gate']}"
        lines.append(f"| {r['summary']} | {files} | {sec} | `{r['verify']}` |")
    lines += ["", _TENANT_MARK, "", "## Tenants (discovered at generation time)", ""]
    tenants = _tenant_rows()
    if not tenants:
        lines.append("(none discovered — desk-only tree)")
    else:
        lines += [
            "A tenant's gate sections run inside `python3 -m desk.verify gate`, labeled",
            "`<tenant>:<section>`; `python3 -m desk.verify tenants` shows discovery + seal status.",
            "",
            "| Feature | Files | Eval section(s) | Verify |",
            "|---|---|---|---|",
        ]
        for t in tenants:
            lines.append(f"| `{t['name']}` v{t['version']} | `{t['dir']}` "
                         f"| `{t['name']}:*` via `{t['eval_hook']}` | `{_GATE_VERIFY}` |")
    lines += ["", "<!-- tenants:end -->", ""]
    return "\n".join(lines)


def _split_at_tenants(text: str) -> Tuple[str, str]:
    i = text.find(_TENANT_MARK)
    return (text, "") if i < 0 else (text[:i], text[i:])


def _tenant_names_in(tenant_part: str) -> set:
    return set(re.findall(r"^\| `([^`]+)` ", tenant_part, re.M))


def feature_map_status() -> Tuple[str, str]:
    """('fresh'|'core-fresh'|'stale'|'missing', detail). 'core-fresh' = the core part matches but
    the discovered tenant set differs from generation time (a desk-only tree) — not drift."""
    want = render_feature_map()
    path = repo_path(*_FM_PATH)
    if not os.path.exists(path):
        return "missing", f"{path} does not exist — run `python3 -m desk.verify feature-map --write`"
    with open(path) as f:
        have = f.read()
    if have == want:
        return "fresh", ""
    w_core, w_ten = _split_at_tenants(want)
    h_core, h_ten = _split_at_tenants(have)
    if w_core != h_core:
        return "stale", "core part drifted — regenerate: `python3 -m desk.verify feature-map --write`"
    if _tenant_names_in(w_ten) != _tenant_names_in(h_ten):
        return "core-fresh", "tenant set differs from generation time (desk-only tree?) — core verified"
    return "stale", "tenant part drifted — regenerate: `python3 -m desk.verify feature-map --write`"


def cmd_feature_map(as_json: bool, write: bool, check: bool) -> int:
    if check:
        status, detail = feature_map_status()
        ok = status in ("fresh", "core-fresh")
        if as_json:
            print(json.dumps({"ok": ok, "status": status, "detail": detail,
                              "doc": "/".join(_FM_PATH)}))
        else:
            print(f"feature-map: {status}" + (f" — {detail}" if detail else ""))
        return 0 if ok else 1
    text = render_feature_map()
    if write:
        path = repo_path(*_FM_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        print(json.dumps({"written": path, "bytes": len(text)}) if as_json
              else f"wrote {path} ({len(text)} bytes)")
        return 0
    if as_json:
        print(json.dumps({"doc": text}))
    else:
        print(text, end="")
    return 0


# ── entrypoint ─────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="exactly one JSON object on stdout (diagnostics on stderr)")
    ap = argparse.ArgumentParser(
        prog="python3 -m desk.verify",
        description="Agent-facing verification CLI: gate | tenants | tick | health | feature-map. "
                    "Read-only; exit codes: 0 ok, 1 failed, 2 usage, 3 refused preflight.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gate", parents=[common],
                   help="run the offline golden gate (eval-fast); exit code mirrors it")
    sub.add_parser("tenants", parents=[common],
                   help="registry summary: bases, tenants, enabled specs, per-row seal status")
    t = sub.add_parser("tick", parents=[common],
                       help="one paper driver tick; REFUSES on an empty capture store")
    t.add_argument("--dry-run", action="store_true",
                   help="report what WOULD fire (rows, store, live gates); writes nothing")
    sub.add_parser("health", parents=[common],
                   help="read-only GET of the dashboard's /api/health")
    fm = sub.add_parser("feature-map", parents=[common],
                        help="generate docs/feature-map.md (stdout unless --write)")
    x = fm.add_mutually_exclusive_group()
    x.add_argument("--write", action="store_true", help="rewrite docs/feature-map.md in place")
    x.add_argument("--check", action="store_true", help="exit 1 when the committed doc drifted")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "gate":
            return cmd_gate(args.json)
        if args.cmd == "tenants":
            return cmd_tenants(args.json)
        if args.cmd == "tick":
            return cmd_tick(args.json, args.dry_run)
        if args.cmd == "health":
            return cmd_health(args.json)
        if args.cmd == "feature-map":
            return cmd_feature_map(args.json, args.write, args.check)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": repr(e), "cmd": args.cmd}))
        else:
            print(f"ERROR: {e!r}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
