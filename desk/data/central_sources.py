"""data/central_sources.py — the LATE-BOUND registry of tenant external-data sources.

The desk framework owns the REGISTRY ONLY: names mapped to tenant-registered callables. It never
imports, names, or wraps a concrete source, and it is SIGNATURE-AGNOSTIC — a source's call shape is
a private contract between the tenant code that registers it and the tenant code that calls it
(data fetchers are tenant infrastructure; a tenant registers its own sources from its own modules
at import time). Lookups on an unregistered name return None, so every caller fails soft on a
desk-only tree.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

_SOURCES: Dict[str, Callable] = {}


def register_source(name: str, fn: Callable) -> None:
    """Register (or replace) a named source. Called by tenants at import time."""
    _SOURCES[name] = fn


def get_source(name: str) -> Optional[Callable]:
    """The registered source for `name`, or None (caller falls back / skips)."""
    return _SOURCES.get(name)
