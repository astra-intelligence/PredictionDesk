"""driver/poslock.py — ONE cross-process lock per strategy's persisted position state.

Every read-modify-write of positions_<name>.json (and the live_orders_<name>.json written in the
same critical sections) MUST hold this lock: the strategy driver's fire/settle/reconcile passes and
the exit monitor run as SEPARATE processes against the same files, and an unlocked window lets an
exit-close be overwritten by a stale in-memory copy — the closed position resurrects, the monitor
re-sells it next pass, and realized PnL misbooks. The lock file is positions_<name>.json.lock
(kernel fcntl.flock — advisory but cross-process), the SAME file for both processes by construction:
the path comes from strategy_driver._positions_path, so a test that repoints DRIVER_DIR repoints the
lock with it.

Fail-soft: if the lock file cannot be opened (read-only fs, permissions) the context proceeds
UNLOCKED rather than blocking all trading — same stance as the exit monitor's original hand-rolled
lock. A blocked flock waits indefinitely; callers under a SIGALRM deadline (the exit monitor's 180s
self-kill) get released by their own alarm.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager


@contextmanager
def file_lock(path: str):
    """Exclusive fcntl.flock on `path` for the duration of the block. Fail-soft on open errors."""
    lk = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)   # DESK_DATA_DIR target may not exist yet
        lk = open(path, "w")
        fcntl.flock(lk, fcntl.LOCK_EX)
    except OSError:
        lk = None
    try:
        yield
    finally:
        if lk is not None:
            try:
                fcntl.flock(lk, fcntl.LOCK_UN)
                lk.close()
            except Exception:
                pass


def positions_lock(name: str):
    """The per-strategy position-state lock: flock on positions_<name>.json.lock. Import is lazy to
    avoid a module cycle (strategy_driver imports this module at top level)."""
    from desk.driver.strategy_driver import _positions_path
    return file_lock(_positions_path(name) + ".lock")
