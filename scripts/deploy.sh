#!/bin/bash
# Re-copy cron scripts into ~/.hermes/scripts/ so hermes cron can find them.
# Hermes cron rejects symlinks that resolve outside ~/.hermes/scripts/ as path
# traversal, so we copy instead. Run this after every `git pull` in /opt/hermes-aeon.

set -e

SCRIPTS_DIR="${HOME}/.hermes/scripts"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$SCRIPTS_DIR"

copied=0
for f in bookmarks_fetch derive_profile discover_rss discover_x digest github_fetch oura_fetch hf_papers; do
  src="$SOURCE_DIR/${f}.py"
  dst="$SCRIPTS_DIR/aeon_${f}.py"
  if [ -f "$src" ] && ( [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" ); then
    cp "$src" "$dst"
    copied=$((copied + 1))
  fi
done

# _common.py is imported by all of them
src="$SOURCE_DIR/_common.py"
dst="$SCRIPTS_DIR/_common.py"
if [ -f "$src" ] && ( [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" ); then
  cp "$src" "$dst"
  copied=$((copied + 1))
fi

echo "deployed $copied changed file(s) to $SCRIPTS_DIR"
