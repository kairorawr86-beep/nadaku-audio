#!/bin/bash
# checkpoint_push.sh — push hasil sinkronisasi ke repo GitHub nadaku-audio.
# Token dibaca dari file token (chmod 600), TIDAK pernah di-embed di source.
# Path dikontrol env agar bisa dipakai di VPS maupun container panel.
set -e
LABEL="$1"
TOKEN_FILE="${TOKEN_FILE:-/root/.github_token}"
REPO_DIR="${REPO_DIR:-/root/nadaku_sync/results_repo}"
SRC="${SRC:-/root/nadaku_sync/out}"
TOKEN=$(cat "$TOKEN_FILE")
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --depth 1 "https://x-access-token:${TOKEN}@github.com/kairorawr86-beep/nadaku-audio.git" "$REPO_DIR" >/dev/null 2>&1
fi
cd "$REPO_DIR"
git config user.email "pipeline@nadakufy.my.id"
git config user.name "nadaku-sync-pipeline"
mkdir -p "sync-results/${LABEL}"
cp "$SRC"/*.json "sync-results/${LABEL}/" 2>/dev/null || true
git add "sync-results/${LABEL}" >/dev/null 2>&1
if git diff --cached --quiet; then echo "no changes"; exit 0; fi
git commit -q -m "sync-results: ${LABEL} checkpoint $(date -u +%Y-%m-%dT%H:%MZ) ($(ls "$SRC"/*.json 2>/dev/null | wc -l) files)"
git push -q origin HEAD 2>&1 | grep -v "^Everything" | head -2 || true
echo "PUSHED $(ls "$SRC"/*.json 2>/dev/null | wc -l) results dari ${LABEL}"
