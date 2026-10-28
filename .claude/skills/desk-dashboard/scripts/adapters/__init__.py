"""Dashboard adapters. ONE trade adapter: ledger.py (--source backtest|paper|live); opportunities
adapters (tenant-specific live screens) implement the contract in CONTRACT.md.

Shared display-timezone resolution: every rendered timestamp uses env DESK_TZ (IANA name,
default UTC). Storage stays unix seconds — DESK_TZ changes RENDERING only.
"""
from __future__ import annotations

import os
from datetime import timezone


def desk_tz():
    """tzinfo for env DESK_TZ (IANA name, e.g. 'Europe/London'); UTC when unset/unresolvable."""
    name = (os.environ.get("DESK_TZ") or "UTC").strip() or "UTC"
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def desk_tz_name() -> str:
    """The IANA tz name rendered pages should use client-side (Intl timeZone), UTC fallback."""
    tz = desk_tz()
    return "UTC" if tz is timezone.utc else str(tz)


def tz_label(dt) -> str:
    """Short label for a rendered timestamp (e.g. 'UTC', 'BST', '+04') from an aware datetime."""
    return dt.strftime("%Z") or "UTC"
