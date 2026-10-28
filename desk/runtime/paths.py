"""runtime/paths.py — ONE resolver for every runtime-data path the desk reads or writes.

The failure class this kills: cwd-relative literals ("desk/data/capture", "desk/sim/spread_model.json")
silently resolve against WHEREVER the process was launched — run a daemon or a one-off from any
directory other than the repo root and it reads empty stores / writes state into the wrong tree, with
no error. Every runtime-data path now goes through data_path(), which anchors to THIS checkout via
__file__ by default and is relocatable with ONE env var:

  DESK_DATA_DIR          absolute root for runtime data. When set, data_path("data", "capture") ->
                         $DESK_DATA_DIR/data/capture, data_path("driver") -> $DESK_DATA_DIR/driver,
                         etc. — the desk-tree-relative layout, re-rooted (mirrors the VPS data-dir
                         split; the dirs are created on first write).
  (unset — the default)  data_path(...) -> <repo>/desk/<...>: byte-identical to the historical
                         repo-root-cwd behavior, so the existing symlinked dirs (desk/data/capture,
                         desk/reports/out, ...) keep working as the default target.

CODE and CONFIG paths (strategy playbooks, capture_config.json, .env) are NOT data: they stay in the
checkout and resolve via repo_path() regardless of DESK_DATA_DIR.

The env var is read PER CALL (not at import) so a test or a launcher that sets it before first use is
honored; module-level constants computed from these helpers are evaluated at that module's import.
"""
from __future__ import annotations

import os

# this file lives at <repo>/desk/runtime/paths.py
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def data_root() -> str:
    """Root for runtime data: $DESK_DATA_DIR when set, else <repo>/desk (__file__-anchored)."""
    env = os.environ.get("DESK_DATA_DIR")
    return env if env else os.path.join(REPO_ROOT, "desk")


def data_path(*rel: str) -> str:
    """Absolute path of a runtime-data file/dir, desk-tree-relative parts: data_path('tracker',
    'ledger.json') -> <data_root>/tracker/ledger.json."""
    return os.path.join(data_root(), *rel)


def repo_path(*rel: str) -> str:
    """Absolute path INSIDE this checkout (code/config — never relocated by DESK_DATA_DIR)."""
    return os.path.join(REPO_ROOT, *rel)


def resolve_data_rel(p: str) -> str:
    """Resolve a possibly-relative runtime-data path from CONFIG (e.g. CaptureConfig.out_dir).
    Absolute -> unchanged. Relative legacy 'desk/<x>' -> data_path(<x>) (so the default equals the
    historical repo-root-cwd behavior and DESK_DATA_DIR relocates it). Any other relative -> under
    data_root()."""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    norm = p.replace("\\", "/").rstrip("/")
    parts = [x for x in norm.split("/") if x not in ("", ".")]
    if parts and parts[0] == "desk":
        parts = parts[1:]
    return data_path(*parts) if parts else data_root()
