#!/bin/bash
# Release notes from the commit messages since a tag (Conventional Commits: feat:, fix:, feat!: ...), grouped like autobrr's.
# Usage: scripts/release-notes.sh <previous-tag> [end-ref]     e.g.  gh release create v1.1 --notes "$(scripts/release-notes.sh v1.0)"
set -euo pipefail
from=${1:?"usage: $0 <previous-tag> [end-ref]"}
log=$(git log --no-merges --pretty='%s' "$from..${2:-HEAD}")
BREAKING='^[a-z]+(\([^)]*\))?!:'
FEAT='^feat(\([^)]*\))?:'
FIX='^fix(\([^)]*\))?:'

section() {  # title, lines
  [ -n "$2" ] || return 0
  printf '### %s\n' "$1"
  sed 's/^/* /' <<<"$2"
  printf '\n'
}
pick() { grep -E "$1" <<<"$log" || true; }

section "Breaking Changes" "$(pick "$BREAKING")"
section "New Features" "$(pick "$FEAT" | grep -v -E "$BREAKING" || true)"
section "Bug fixes" "$(pick "$FIX" | grep -v -E "$BREAKING" || true)"
section "Other work" "$(grep -v -E "$BREAKING|$FEAT|$FIX" <<<"$log" || true)"
