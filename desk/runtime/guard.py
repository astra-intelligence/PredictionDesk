"""runtime/guard.py — the job guard every periodic desk job runs FIRST.

Four observed failure modes, all covered IN-PROCESS (this Mac has no `timeout`/`flock` binaries):
  zombie    — signal.alarm(deadline): a hung run self-kills; the process supervisor fires the next tick.
  stacking  — fcntl non-blocking lock on /tmp/desk_<repo-hash>_<job>.lock: a still-running previous tick
              makes the new one exit 0 immediately (skip, never overlap).
  stale-code daemon — not code: supervisor units must run committed code from THIS checkout
              (enforced by run_eval.check_supervisor).
  env contamination — every run logs its effective DECLARED flags, so a run made under a
              behavior flag is machine-identifiable from the log, not archaeology.

Usage (first lines of any `python3 -m desk.*` job entrypoint):

    from desk.runtime.guard import job_guard
    job_guard("driver", deadline_s=1500)
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import sys

DECLARED_FLAGS = ("LIVE_TRADING_ENABLED", "DESK_TAKER_FEES_ENABLED")
_LOCKS = {}                      # keep lock fds referenced for the process lifetime


def job_guard(job: str, deadline_s: int, declared_flags=DECLARED_FLAGS) -> None:
    """Singleton lock + hard deadline + effective-env log. Exits 0 (skip tick) if already running."""
    # Lock is scoped to THIS repo checkout (root-path hash) so two desks on one machine never share locks.
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    scope = hashlib.sha256(root.encode()).hexdigest()[:8]
    lk = open(f"/tmp/desk_{scope}_{job}.lock", "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{job}] previous run still alive -> skipping this tick", flush=True)
        sys.exit(0)
    _LOCKS[job] = lk
    lk.truncate(0); lk.write(str(os.getpid())); lk.flush()
    if deadline_s:
        signal.alarm(int(deadline_s))          # SIGALRM default = terminate; the supervisor relaunches next tick
    print(f"[{job}] env: " + " ".join(f"{k}={os.environ.get(k)}" for k in declared_flags), flush=True)


def code_ref() -> str:
    """Current git commit (fail-soft 'unknown') — THE one implementation; run/ledger metadata callers
    share it so provenance stamps can't drift."""
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def declared_env() -> dict:
    """The declared behavior flags snapshot every provenance stamp carries (contamination audit)."""
    return {k: os.environ.get(k) for k in DECLARED_FLAGS}


def rotate_log(path: str, max_mb: float = 20.0, backups: int = 2) -> None:
    """Size-capped rotation for the supervisor's stdout/stderr log files (newsyslog needs root — rejected).
    Call at job start on the job's own log paths; a no-op below the cap."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_mb * 1024 * 1024:
            return
        for i in range(backups - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
    except Exception:
        pass
