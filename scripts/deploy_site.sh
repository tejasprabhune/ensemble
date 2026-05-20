#!/usr/bin/env bash
# Bake the demo trace, then rsync the site into the sister
# tejasprabhune.github.io repo at public/ensemble/. After this script
# runs you still need to commit and push in the sister repo to trigger
# the GitHub Pages workflow.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SISTER="${SISTER_REPO:-$(cd "$HERE/../tejasprabhune.github.io" && pwd)}"
TARGET="$SISTER/public/ensemble"

echo "ensemble repo: $HERE"
echo "sister repo:   $SISTER"
echo "target path:   $TARGET"

if [ ! -d "$SISTER" ]; then
  echo "sister repo not found at $SISTER" >&2
  echo "set SISTER_REPO to the right path and re-run." >&2
  exit 1
fi

echo "==> baking deterministic refund_storm trace"
(cd "$HERE" && uv run python examples/plank/bake_trace.py)

echo "==> rendering docs/reference/*.md -> site/reference/*.html"
(cd "$HERE" && uv run python scripts/render_reference.py)

echo "==> rsyncing site/ -> $TARGET"
mkdir -p "$TARGET"
rsync -av --delete \
  --exclude='.DS_Store' \
  "$HERE/site/" "$TARGET/"

echo
echo "next steps:"
echo "  cd $SISTER"
echo "  npm run build          # optional local preview"
echo "  git add public/ensemble"
echo "  git commit -m 'publish ensemble'"
echo "  git push               # triggers GH Pages workflow"
echo
echo "site will be live at: https://tejasprabhune.github.io/ensemble/"
