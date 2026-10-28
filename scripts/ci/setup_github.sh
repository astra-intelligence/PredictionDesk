#!/usr/bin/env bash
# setup_github.sh — idempotent post-push bootstrap of the agent-harness substrate on GitHub.
# Run by the PM AFTER the repo seed push and AFTER the harness files (.github/, scripts/ci/) are
# on dev. Safe to re-run: every step creates-or-updates.
#
#   REPO=owner/name ./scripts/ci/setup_github.sh          (default: stranger00135/PredictionDesk)
#
# What it does:
#   1. preflight: gh auth, scopes, GitHub-plan check (branch protection on a PRIVATE user repo
#      needs GitHub Pro — printed as an explicit check, not assumed)
#   2. labels (tier/lane/risk/flow/fleet)
#   3. `dev` branch (created from main if absent)
#   4. branch protection: dev (required checks + code owners, 0 approvals so T2 can auto-merge)
#      and main (promotion PRs: 1 approval + linear history)
#   5. Projects v2 kanban: Stage/Tier/Lane fields, repo link, PROJECT_URL repo variable
#   6. secrets: SCRUB_PATTERNS from the private workspace term list (never committed publicly);
#      ANTHROPIC_API_KEY is NEVER set here — gated on the owner's key rotation, printed as a step
#   7. prints the remaining MANUAL steps (board views, auto-add workflow, SHA-pinning actions)
set -euo pipefail

REPO="${REPO:-stranger00135/PredictionDesk}"
OWNER="${REPO%%/*}"
PROJECT_TITLE="${PROJECT_TITLE:-PredictionDesk harness}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REQUIRED_CHECKS='["eval-fast","py-compile","scrub","openspec-lint","spec-verifier"]'

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

# ── 1. preflight ──────────────────────────────────────────────────────────────────────────────
say "preflight: gh auth + repo + plan"
gh auth status >/dev/null || { echo "FATAL: gh not authenticated"; exit 1; }
gh repo view "$REPO" --json name >/dev/null || { echo "FATAL: cannot see $REPO"; exit 1; }
PRIVATE="$(gh repo view "$REPO" --json isPrivate -q .isPrivate)"
PLAN="$(gh api user -q '.plan.name' 2>/dev/null || echo unknown)"
note "repo private: $PRIVATE · account plan: $PLAN"
if [ "$PRIVATE" = "true" ] && [ "$PLAN" != "pro" ]; then
  note "CHECK: branch protection on a PRIVATE user repo requires GitHub Pro."
  note "       Plan reads as '$PLAN' — protection PUTs below may 403 until Pro is active (~\$4/mo)."
fi
if ! gh auth status 2>&1 | grep -q "project"; then
  note "CHECK: gh token may lack the 'project' scope — if step 5 fails: gh auth refresh -s project"
fi

# ── 2. labels ─────────────────────────────────────────────────────────────────────────────────
say "labels"
mklabel() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null \
  && note "label $1"; }
mklabel "tier:T0"       "b60205" "consequential: Tiger at gate 1 (spec) and gate 2 (merge)"
mklabel "tier:T1"       "d93f0b" "standard: PM approves spec and merge"
mklabel "tier:T2"       "0e8a16" "routine: machine gates + independent verifier; auto-merge candidate"
mklabel "lane:L"        "1d76db" "needs local runtime: capture data, paper stack, screenshots, seals"
mklabel "lane:G"        "5319e7" "offline-verifiable: eval-fast + static evidence suffices"
mklabel "risk:core"     "b60205" "money semantics, eval/golden integrity, live path, contracts"
mklabel "risk:std"      "fbca04" "framework behavior inside existing contracts"
mklabel "risk:low"      "c2e0c6" "docs, copy, isolated UI"
mklabel "needs:tiger"   "b60205" "waiting on a Tiger decision (PM batches these)"
mklabel "needs:spec"    "fbca04" "triaged; awaiting its OpenSpec change"
mklabel "spec:approved" "0e8a16" "gate 1 passed for this change at its tier"
mklabel "blocked"       "000000" "worker escalated after 2 self-fix attempts (see last comment)"
mklabel "agent:build"   "0e8a16" "dispatch a lane-G build worker on this issue"
mklabel "agent:hard"    "5319e7" "upgrade the worker model class for this task"
mklabel "fleet:claude"  "bfd4f2" "executed by the Claude lane-G fleet"
mklabel "fleet:factory" "bfdadc" "executed by a Factory droid (A/B trial lane)"
mklabel "evidence:ui"   "c5def5" "evidence contract includes screenshots"

# ── 3. dev branch ─────────────────────────────────────────────────────────────────────────────
say "dev branch"
if gh api "repos/$REPO/branches/dev" >/dev/null 2>&1; then
  note "dev exists"
else
  MAIN_SHA="$(gh api "repos/$REPO/git/ref/heads/main" -q .object.sha)"
  gh api -X POST "repos/$REPO/git/refs" -f ref=refs/heads/dev -f sha="$MAIN_SHA" >/dev/null
  note "created dev at $MAIN_SHA"
fi

# ── 4. branch protection ──────────────────────────────────────────────────────────────────────
# dev: PRs only; required checks strict (up-to-date); code-owner review with 0 blanket approvals
#      (owned paths need the owner; unowned T2 paths can auto-merge on green).
# main: promotion PRs — 1 approval + code owners + linear history + same checks.
say "branch protection (needs Pro while the repo is private)"
protect() {
  local branch="$1" reviews="$2" linear="$3"
  if gh api -X PUT "repos/$REPO/branches/$branch/protection" --input - >/dev/null <<EOF
{
  "required_status_checks": { "strict": true, "contexts": $REQUIRED_CHECKS },
  "enforce_admins": true,
  "required_pull_request_reviews": $reviews,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": $linear,
  "required_conversation_resolution": true
}
EOF
  then note "protected $branch"
  else note "WARN: protection PUT failed for $branch (missing Pro on a private repo, or missing admin scope)"
  fi
}
protect dev  '{ "require_code_owner_reviews": true, "required_approving_review_count": 0 }' false
protect main '{ "require_code_owner_reviews": true, "required_approving_review_count": 1 }' true
note "required checks: $REQUIRED_CHECKS"
note "note: check names appear selectable only after each workflow has run at least once"

# ── 5. Projects v2 kanban ─────────────────────────────────────────────────────────────────────
# Tiger's visual of what agents are working on: columns = the Stage field, one view grouped by
# Stage, plus a needs:tiger filtered view. Views + built-in auto-add cannot be created via API —
# printed as manual steps below.
say "Projects v2 board: $PROJECT_TITLE"
export PROJECT_TITLE_WANTED="$PROJECT_TITLE"
PROJ_NUM="$(gh project list --owner "$OWNER" --format json --limit 100 \
  | python3 -c 'import json,sys,os
d=json.load(sys.stdin)
t=os.environ["PROJECT_TITLE_WANTED"]
print(next((str(p["number"]) for p in d.get("projects",[]) if p["title"]==t), ""))')"
if [ -z "$PROJ_NUM" ]; then
  PROJ_NUM="$(gh project create --owner "$OWNER" --title "$PROJECT_TITLE" --format json -q .number)"
  note "created project #$PROJ_NUM"
else
  note "project #$PROJ_NUM exists"
fi
have_field() {
  gh project field-list "$PROJ_NUM" --owner "$OWNER" --format json --limit 100 \
    | python3 -c 'import json,sys
name=sys.argv[1]
d=json.load(sys.stdin)
print("yes" if any(f.get("name")==name for f in d.get("fields",[])) else "no")' "$1"
}
mkfield() {
  local name="$1" opts="$2"
  if [ "$(have_field "$name")" = "yes" ]; then note "field $name exists"; else
    gh project field-create "$PROJ_NUM" --owner "$OWNER" --name "$name" \
      --data-type SINGLE_SELECT --single-select-options "$opts" >/dev/null
    note "field $name: $opts"
  fi
}
mkfield "Stage" "Triage,Spec,Spec review,In progress,Evidence+Verifier,Merge gate,Done"
mkfield "Tier"  "T0,T1,T2"
mkfield "Lane"  "L,G"
gh project link "$PROJ_NUM" --owner "$OWNER" --repo "$REPO" >/dev/null 2>&1 || true
PROJECT_URL="https://github.com/users/$OWNER/projects/$PROJ_NUM"
gh variable set PROJECT_URL --repo "$REPO" --body "$PROJECT_URL" >/dev/null
note "linked to $REPO · PROJECT_URL variable set: $PROJECT_URL"

# ── 6. secrets ────────────────────────────────────────────────────────────────────────────────
say "secrets"
PATTERNS_FILE="$ROOT/scripts/ci/scrub_patterns.txt"
if [ -f "$PATTERNS_FILE" ]; then
  JOINED="$(grep -vE '^[[:space:]]*(#|$)' "$PATTERNS_FILE" | paste -sd'|' -)"
  gh secret set SCRUB_PATTERNS --repo "$REPO" --body "$JOINED"
  note "SCRUB_PATTERNS set from the private workspace list ($(echo "$JOINED" | tr '|' '\n' | wc -l | tr -d ' ') terms)"
else
  note "WARN: $PATTERNS_FILE not found (not running from the workspace?) — SCRUB_PATTERNS not set;"
  note "      non-fork scrub CI will FAIL CLOSED until it is."
fi
if [ -n "${PROJECTS_PAT:-}" ]; then
  gh secret set PROJECTS_PAT --repo "$REPO" --body "$PROJECTS_PAT"
  note "PROJECTS_PAT set (project auto-add workflow active)"
else
  note "PROJECTS_PAT not provided — project auto-add stays inert (see manual steps)"
fi
note "ANTHROPIC_API_KEY: NOT set by this script — GATED on the owner rotating the key first."
note "  after rotation: gh secret set ANTHROPIC_API_KEY --repo $REPO"

# ── 7. manual steps ───────────────────────────────────────────────────────────────────────────
say "remaining MANUAL steps (no API exists for these)"
cat <<'EOS'
   a. Board: open the project -> view 1 -> layout Board -> group by "Stage"
      (columns: Triage / Spec / Spec review / In progress / Evidence+Verifier / Merge gate / Done)
   b. View "Spec review - Tiger": duplicate the board view, filter: label:needs:tiger
   c. Auto-add: EITHER project settings -> Workflows -> "Auto-add to project" -> enable for the
      repo with filter `is:issue,is:pr is:open`, OR set the PROJECTS_PAT secret (classic PAT,
      scopes: project, repo) and the project-add workflow takes over
   d. After every workflow has run once, confirm the five required checks resolve by name on
      dev/main protection (eval-fast, py-compile, scrub, openspec-lint, spec-verifier)
   e. Harden: SHA-pin third-party actions (gitleaks-action, add-to-project) in .github/workflows
   f. ANTHROPIC_API_KEY after rotation (step 6 note) -- claude.yml and verifier.yml are inert
      until it exists
EOS
say "done"
