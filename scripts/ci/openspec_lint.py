#!/usr/bin/env python3
"""openspec-lint — required PR check (harness pipeline step 6; design §4 + §6.1.4).

Fail-closed validator that a PR is attached to the spec layer at the rigor its tier declares:

  1. exactly one tier label (tier:T0 | tier:T1 | tier:T2) on the PR;
  2. the PR body names a real OpenSpec change (template field "OpenSpec change:") that exists in
     the PR tree: openspec/changes/<name>/ (archive/ accepted for post-archive follow-ups);
  3. the change carries tier-appropriate artifacts:
       T2: proposal.md (proposal-only is the T2 contract)
       T1: + tasks.md
       T0: + design.md (full spec depth)
  4. the proposal honors the repo rule (openspec/config.yaml): it names the eval gate section(s)
     it adds/changes, or says "none" with a justification;
  5. gate-1 (spec review) state matches the tier:
       T0: an APPROVED review by the repo OWNER on this PR, or the `spec:approved` label
           (the PM applies it only after the owner approved the spec — e.g. on a separate spec PR)
       T1: any APPROVED review, or the `spec:approved` label
       T2: schema-valid spec only; no human gate (auto)

Runs inside GitHub Actions with GITHUB_TOKEN (read-only use). stdlib only.
Exit: 0 pass, 1 fail, 2 usage/environment error.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

TIERS = {"tier:T0", "tier:T1", "tier:T2"}


def api(path: str):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    req = urllib.request.Request(
        f"{base}/repos/{repo}{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def pr_number() -> int:
    if os.environ.get("PR_NUMBER"):
        return int(os.environ["PR_NUMBER"])
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        ev = json.load(f)
    if "pull_request" in ev:
        return int(ev["pull_request"]["number"])
    if "number" in ev:
        return int(ev["number"])
    print("openspec-lint: cannot determine PR number", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    n = pr_number()
    pr = api(f"/pulls/{n}")
    body = pr.get("body") or ""
    labels = {l["name"] for l in pr.get("labels", [])}
    errors: list[str] = []
    notes: list[str] = []

    # 1. exactly one tier label
    tiers = sorted(labels & TIERS)
    tier = tiers[0] if len(tiers) == 1 else None
    if len(tiers) != 1:
        errors.append(
            f"PR must carry exactly one tier label (tier:T0|tier:T1|tier:T2); found: {tiers or 'none'}. "
            "Triage (the PM) assigns the tier per the change-class table."
        )

    # 2. named OpenSpec change
    m = re.search(r"(?im)^[\s>*-]*OpenSpec change:\s*`?([A-Za-z0-9][A-Za-z0-9._/-]*)`?", body)
    change = m.group(1).strip().strip("`") if m else None
    change_dir = None
    if not change or change.lower() in {"none", "n/a", "tbd", "<name>", "name"}:
        errors.append(
            "PR body must name a real OpenSpec change (template line 'OpenSpec change: <name>'). "
            "Every PR, T2 included, is attached to a change — proposal-only suffices for T2."
        )
    else:
        for cand in (f"openspec/changes/{change}", f"openspec/changes/archive/{change}"):
            if os.path.isdir(cand):
                change_dir = cand
                break
        if change_dir is None:
            errors.append(f"named change '{change}' not found under openspec/changes/ in this tree")

    # 3. tier-appropriate artifacts  4. eval-section rule
    if change_dir:
        proposal = os.path.join(change_dir, "proposal.md")
        if not os.path.isfile(proposal):
            errors.append(f"{change_dir}/proposal.md missing")
        else:
            text = open(proposal, encoding="utf-8", errors="replace").read()
            names_section = re.search(r"(?i)eval[\s-]*(gate\s*)?section", text) or re.search(
                r"(?i)section[s]?\s*[:：]", text
            )
            says_none = re.search(r"(?i)section[s]?\s*[:：]?\s*none\b.{10,}", text)
            if not (names_section or says_none):
                errors.append(
                    "proposal.md must name the eval gate section(s) it adds/changes, or say "
                    "'none' with a justification (openspec/config.yaml proposal rule)"
                )
        if tier in {"tier:T0", "tier:T1"} and not os.path.isfile(os.path.join(change_dir, "tasks.md")):
            errors.append(f"{tier} requires {change_dir}/tasks.md")
        if tier == "tier:T0" and not os.path.isfile(os.path.join(change_dir, "design.md")):
            errors.append(f"tier:T0 requires {change_dir}/design.md (full spec depth)")

    # 5. gate-1 state per tier
    if tier in {"tier:T0", "tier:T1"}:
        owner = os.environ["GITHUB_REPOSITORY"].split("/")[0]
        reviews = api(f"/pulls/{n}/reviews")
        approved_by = {r["user"]["login"] for r in reviews if r.get("state") == "APPROVED"}
        if tier == "tier:T0":
            ok = owner in approved_by or "spec:approved" in labels
            if not ok:
                errors.append(
                    f"tier:T0 gate 1 unsatisfied: needs an APPROVED review by @{owner} on this PR, "
                    "or the spec:approved label (applied only after the owner approved the spec)"
                )
        else:
            ok = bool(approved_by) or "spec:approved" in labels
            if not ok:
                errors.append(
                    "tier:T1 gate 1 unsatisfied: needs an APPROVED review or the spec:approved label"
                )
    elif tier == "tier:T2":
        notes.append("tier:T2 — schema-validated spec only; no human gate (merge gate stays tiered)")

    print(f"openspec-lint: PR #{n} tier={tier or '?'} change={change or '?'}")
    for s in notes:
        print(f"  note: {s}")
    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        return 1
    print("  PASS: spec attachment matches the declared tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
