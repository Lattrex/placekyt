#!/usr/bin/env bash
# Clean, deterministic placeKYT launch. Guarantees a pristine state every run so
# leftover processes / stale bytecode / a held server port can NEVER corrupt a
# session (the "it only works with a magic click sequence" class of bug).
#
# Does, in order:
#   1. Kill any running placeKYT (a stale one holds port 58950; a NEW placeKYT
#      then silently falls back to a random port, so GRC — hardcoded to 58950 —
#      talks to the DEAD process and produces no output).
#   2. Free the server port 58950 (kill whatever still holds it).
#   3. Purge stale __pycache__ under the source tree (so reverted/edited source
#      can't be shadowed by old compiled bytecode).
#   4. Clear the GRC block-parse cache (so GRC re-reads current .block.yml).
#   5. Verify the installed GRC kyttar module matches the repo (warn if drifted
#      — you must run ./gr-kyttar/install.sh to deploy repo changes to GRC).
#   6. Launch placeKYT.
#
# Usage:  ./run.sh            # clean + launch
#         ./run.sh --no-launch # clean only (for scripting)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

SERVER_PORT="${KYTTAR_SERVER_PORT:-58950}"
say() { printf '  %s\n' "$*"; }

echo "placeKYT clean launch"

# --- 1. kill any running placeKYT GUI --------------------------------------
echo "[1/5] stop any running placeKYT"
mapfile -t PIDS < <(pgrep -f "placekyt/main.py" 2>/dev/null || true)
if [ "${#PIDS[@]}" -gt 0 ]; then
  for p in "${PIDS[@]}"; do say "killing placeKYT pid $p"; kill "$p" 2>/dev/null || true; done
  sleep 1
  for p in "${PIDS[@]}"; do kill -9 "$p" 2>/dev/null || true; done
else
  say "none running"
fi

# --- 2. free the server port ------------------------------------------------
echo "[2/5] free server port $SERVER_PORT"
PORT_PIDS="$(ss -tlnp 2>/dev/null | awk -v p=":$SERVER_PORT" '$4 ~ p {print}' | grep -oP 'pid=\K[0-9]+' || true)"
if [ -n "$PORT_PIDS" ]; then
  for p in $PORT_PIDS; do say "killing port holder pid $p"; kill -9 "$p" 2>/dev/null || true; done
  sleep 1
else
  say "port $SERVER_PORT is free"
fi

# --- 3. purge stale bytecode under the source tree -------------------------
echo "[3/5] purge stale __pycache__"
N="$(find placekyt runtime/python gr-kyttar/python -name __pycache__ -type d 2>/dev/null | wc -l)"
find placekyt runtime/python gr-kyttar/python -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
say "removed $N __pycache__ dir(s)"

# --- 4. clear the GRC parse cache ------------------------------------------
echo "[4/5] clear GRC parse cache"
CACHE_BASE="${XDG_CACHE_HOME:-$HOME/.cache}"
rm -rf "$CACHE_BASE"/gnuradio/grc "$CACHE_BASE"/grc_gnuradio 2>/dev/null || true
say "cleared"

# --- 5. verify installed GRC module matches the repo -----------------------
echo "[5/5] verify installed GRC kyttar module == repo"
INST="$(/usr/bin/python3 -c 'from gnuradio import kyttar,os;print(os.path.dirname(kyttar.__file__))' 2>/dev/null || true)"
if [ -n "$INST" ] && [ -d "$INST" ]; then
  DRIFT=0
  for f in gr-kyttar/python/kyttar/*.py; do
    b="$(basename "$f")"
    if [ -f "$INST/$b" ]; then
      if ! cmp -s "$f" "$INST/$b"; then DRIFT=1; say "DRIFT: $b (installed != repo)"; fi
    else
      DRIFT=1; say "MISSING in install: $b"
    fi
  done
  if [ "$DRIFT" = 1 ]; then
    echo
    echo "  ⚠  The GRC-side kyttar module GRC actually runs is OUT OF SYNC with the repo."
    echo "     Run:  ./gr-kyttar/install.sh   (needs sudo) then re-run this script."
  else
    say "installed GRC module is current"
  fi
else
  say "kyttar not importable under system python (GRC may not see the blocks)"
fi

if [ "${1:-}" = "--no-launch" ]; then
  echo "clean complete (--no-launch)"; exit 0
fi

echo
echo "launching placeKYT (server will bind port $SERVER_PORT) ..."
exec .venv/bin/python placekyt/main.py
