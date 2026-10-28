"""render.py — generic dashboard renderer. Takes a normalized SPEC dict and bakes it into the self-contained
template.html (no server, no build step). Adapters (scripts/adapters/*) produce the SPEC; this just renders it.

  from render import render
  render(spec, "out/dashboard.html")

SPEC schema is documented in SKILL.md. The template renders any SPEC that matches it — forward runs, backtests,
anything — so a new data source only needs an adapter, never a template change.
"""
from __future__ import annotations
import json
import os


def render(spec: dict, out_path: str, template_path: str | None = None) -> str:
    tp = template_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
    html = open(tp).read().replace("/*__DATA__*/null", json.dumps(spec))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    return out_path
