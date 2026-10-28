#!/usr/bin/env bash
# scrub.sh — the ONE leak gate for public-bound trees. Shared by two callers (no logic fork):
#
#   release cut:  scripts/release_public.sh runs it against the staging dir before every push
#   per-PR CI:    .github/workflows/scrub.yml runs it against the checkout on every PR
#
# Three checks against TARGET (arg 1, default "."), all fail-closed:
#   1. forbidden-term grep    case-insensitive ERE over every text file; zero hits required
#   2. binary allowlist       every shipped binary must be individually reviewed + allowlisted
#   3. runtime-leftover sweep nothing transient may ship (data/sample is the deliberate exception)
#
# Forbidden-term sources for check 1 (first present wins). The real term list is itself private
# (it names exactly what must never leak), so it is NEVER committed to a public tree:
#   $SCRUB_PATTERNS        one ERE alternation string (CI sets it from the SCRUB_PATTERNS repo
#                          secret; absent on fork PRs by GitHub's secret rules)
#   $SCRUB_PATTERNS_FILE   path to a pattern file: comment/blank lines stripped, rest |-joined
#                          (default: scrub_patterns.txt beside this script when present — the
#                          private workspace keeps the list there; release cuts exclude it)
#   neither                generic secret-shape patterns only; FAILS if SCRUB_REQUIRE_PATTERNS=1
#
# SCRUB_REQUIRE_PATTERNS=1  missing term list is a FAILURE (release cuts; non-fork CI)
# SCRUB_REDACT=1            print file:line only, never the matched text (public CI logs must not
#                           republish the very content the scrub exists to contain)
#
# Exit codes: 0 clean · 2 usage · 3 any check failed
set -euo pipefail

TARGET="${1:-.}"
[ -d "$TARGET" ] || { echo "usage: scrub.sh [DIR]" >&2; exit 2; }
TARGET="$(cd "$TARGET" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── check 1: forbidden-term grep ──────────────────────────────────────────────────────────────
PATTERNS=""
PATTERNS_SRC="none"
if [ -n "${SCRUB_PATTERNS:-}" ]; then
  PATTERNS="$SCRUB_PATTERNS"
  PATTERNS_SRC="env"
else
  PFILE="${SCRUB_PATTERNS_FILE:-$SCRIPT_DIR/scrub_patterns.txt}"
  if [ -f "$PFILE" ]; then
    PATTERNS="$(grep -vE '^[[:space:]]*(#|$)' "$PFILE" | paste -sd'|' -)"
    PATTERNS_SRC="$PFILE"
  fi
fi
if [ -z "$PATTERNS" ]; then
  if [ "${SCRUB_REQUIRE_PATTERNS:-0}" = "1" ]; then
    echo "SCRUB FAIL: no forbidden-term list (SCRUB_PATTERNS env / scrub_patterns.txt) and SCRUB_REQUIRE_PATTERNS=1"
    exit 3
  fi
  echo "scrub: NO private term list available — generic + structural checks only (fork-PR mode)"
fi

# Generic secret shapes, always on (these are PATTERNS, not secrets; this script excludes itself
# from its own grep). .env.example is exempt from THIS grep only — it is a placeholder template.
GENERIC='-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|x-access-token:[A-Za-z0-9_-]+@'

# DELIBERATE, reviewed exceptions (path-anchored, never term-anchored). Each entry documents a
# hit the owner accepted on purpose; everything else stays zero-tolerance.
# (none right now — config.yml routes strategy ideas to the in-repo sample tenants since
#  2026-10-27, so the private-repo-name exception is retired; keep this mechanism for future use)
ALLOW_RE='^$'

GREP_COMMON=(-rniEI --exclude-dir=.git --exclude-dir=__pycache__ --exclude=scrub.sh --exclude=scrub_patterns.txt)

# grep exit codes: 0 match, 1 no match, 2 error. An error must FAIL the scrub (never fail-open).
HITS_TERMS="$(mktemp /tmp/scrub-hits.XXXXXX)"
HITS_GENERIC="$(mktemp /tmp/scrub-hits.XXXXXX)"
rc_terms=1
if [ -n "$PATTERNS" ]; then
  set +e; grep "${GREP_COMMON[@]}" -e "$PATTERNS" "$TARGET" > "$HITS_TERMS"; rc_terms=$?; set -e
fi
set +e; grep "${GREP_COMMON[@]}" --exclude=.env.example -e "$GENERIC" "$TARGET" > "$HITS_GENERIC"; rc_generic=$?; set -e
if [ "$rc_terms" -ge 2 ] || [ "$rc_generic" -ge 2 ]; then
  echo "SCRUB FAIL: grep errored (terms rc=$rc_terms, generic rc=$rc_generic) — fail-closed"
  exit 3
fi
ALLHITS="$(cat "$HITS_TERMS" "$HITS_GENERIC" | grep -vE "$ALLOW_RE" | grep -v '^$' || true)"
rm -f "$HITS_TERMS" "$HITS_GENERIC"
if [ -n "$ALLHITS" ]; then
  if [ "${SCRUB_REDACT:-0}" = "1" ]; then
    echo "$ALLHITS" | cut -d: -f1,2
    echo "(matched text redacted: SCRUB_REDACT=1 — re-run the scrub locally to see the content)"
  else
    echo "$ALLHITS"
  fi
  echo "SCRUB FAIL: $(echo "$ALLHITS" | wc -l | tr -d ' ') forbidden-term hit(s) — fix at the source (or add a reviewed path exception) and re-run"
  exit 3
fi
echo "SCRUB PASS: 0 text hits (term list: $PATTERNS_SRC + generic secret shapes)"

# ── check 2: binary allowlist ─────────────────────────────────────────────────────────────────
# Binaries can't be text-grepped meaningfully (compressed bytes false-positive); instead EVERY
# shipped binary must be individually reviewed and allowlisted here — anything else fails.
BIN_ALLOW='^\./(docs/assets/mission-control\.(gif|png)|data/sample/capture/[0-9-]+/[0-9a-fx]+/(book_snapshots|trades)/day\.parquet)$'
BADBINS="$(cd "$TARGET" && find . \( -name .git -o -name __pycache__ \) -prune -o -type f ! -size 0 \
  -exec sh -c 'file -b --mime "$1" | grep -q "charset=binary"' _ {} \; -print | grep -vE "$BIN_ALLOW" || true)"
if [ -n "$BADBINS" ]; then
  echo "$BADBINS"
  echo "BINARY ALLOWLIST FAIL: unreviewed binary file(s)"
  exit 3
fi
echo "binary allowlist: every shipped binary is a reviewed asset/sample"

# ── check 3: runtime-leftover sweep ───────────────────────────────────────────────────────────
# Nothing transient may ship (data/sample is the DELIBERATE exception — the bundled demo capture,
# wallet/tx columns blanked, committed on purpose).
LEFTOVERS="$(find "$TARGET" -name .git -prune -o -path "$TARGET/data/sample" -prune -o \
  \( -name "*.parquet" -o -name "*.err" -o -name "*.out" -o -name "*.pid" -o -name "_heartbeat" \
  -o -name "positions_*.json" -o -name "resting_*.json" -o -name "trades.jsonl" \
  -o -name "PASS.json" -o -name ".env" \) -print | grep -v .env.example || true)"
if [ -n "$LEFTOVERS" ]; then
  echo "$LEFTOVERS"
  echo "RUNTIME-LEFTOVER FAIL"
  exit 3
fi
echo "runtime-leftover sweep: clean"
