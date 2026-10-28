"""eval/api.py — the eval-gate section contract shared by the framework gate and tenant plug-ins.

A gate section is a `Section`: named, hard (gating) or not, accumulating pass/fail checks + notes.
run_eval renders core framework sections and then every discovered tenant's sections (manifest hook
`"eval_checks": "pkg.mod:sections"`, a zero-arg callable returning a list of Section). Tenants import
Section from HERE (stable API), never from run_eval internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Section:
    name: str
    passed: int = 0
    failed: int = 0
    hard: bool = True
    notes: List[str] = field(default_factory=list)

    def check(self, ok: bool, label: str, detail: str = ""):
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.notes.append(f"FAIL {label}: {detail}")
