#!/usr/bin/env bash
# Install / resync the GNURadio "kyttar" out-of-tree module + its GRC block
# definitions so gnuradio-companion (and a python `from gnuradio import kyttar`)
# use the CANONICAL, source-controlled versions in this repo.
#
# WHY THIS EXISTS: the installed package at
#   <python site-packages>/gnuradio/kyttar/
# and the GRC block defs at
#   <gnuradio prefix>/share/gnuradio/grc/blocks/
# can drift from the repo. A stale install silently shadows repo edits (GRC even
# logs "loaded from A overwritten by B"). This script makes the repo the single
# source of truth and deploys it atomically.
#
# CANONICAL SOURCE (this repo, relative to this script):
#   python module : gr-kyttar/python/kyttar/   (DSP + source/sink +
#                   rx_batch + the placekyt_* live-bridge modules)
#   GRC blocks    : gr-kyttar/grc/*.block.yml
#
# Usage:
#   ./install.sh [--dry-run] [--no-sudo] [--py-dest DIR] [--grc-dest DIR]
#
#   --dry-run     print what WOULD be copied; change nothing.
#   --no-sudo     never use sudo (fail instead if a dest needs root).
#   --py-dest     override the python site-packages kyttar dir.
#   --grc-dest    override the GRC blocks dir.
#
# It backs up whatever it replaces to <dest>.bak-<timestamp> and clears the GRC
# parse cache so the new defs are picked up.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PY="$HERE/python/kyttar"
SRC_GRC="$HERE/grc"

DRY_RUN=0
USE_SUDO_OK=1
PY_DEST=""
GRC_DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-sudo) USE_SUDO_OK=0 ;;
    --py-dest) PY_DEST="$2"; shift ;;
    --grc-dest) GRC_DEST="$2"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
run() {
  # run a command, honoring --dry-run; escalate to sudo only if the target is not
  # writable AND sudo is permitted.
  local needs_root="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] $*"; return 0; fi
  if [ "$needs_root" = 1 ] && [ "$USE_SUDO_OK" = 1 ]; then
    sudo "$@"
  else
    "$@"
  fi
}

# --- locate the python site-packages kyttar dir -----------------------------
# Prefer the EXISTING kyttar subpackage's own location: gnuradio's core may load
# from a system dir while `kyttar` is a user-local (~/.local) addition that
# shadows it — that user-local copy is what GRC actually imports, and is where a
# fresh install should land (no root). Only if kyttar isn't importable yet do we
# fall back to a writable gnuradio parent, preferring user-local over system.
if [ -z "$PY_DEST" ]; then
  PY_DEST="$(python3 - <<'PY'
import os, sys, sysconfig
# 1) the kyttar subpackage already on the path — install where it lives.
try:
    from gnuradio import kyttar
    print(os.path.dirname(kyttar.__file__)); sys.exit(0)
except Exception:
    pass
# 2) a gnuradio package dir we can write — prefer the user site over system.
cands = []
try:
    import site
    u = site.getusersitepackages()
    if u: cands.append(u)
except Exception:
    pass
cands.append(sysconfig.get_paths().get("purelib", ""))
try:
    import gnuradio
    cands.insert(0, os.path.dirname(os.path.dirname(gnuradio.__file__)))
except Exception:
    pass
for base in cands:
    if base and os.path.isdir(os.path.join(base, "gnuradio")):
        print(os.path.join(base, "gnuradio", "kyttar")); sys.exit(0)
# 3) last resort: the user site (created if needed).
if cands and cands[0]:
    print(os.path.join(cands[0], "gnuradio", "kyttar"))
PY
)"
  if [ -z "$PY_DEST" ]; then
    echo "ERROR: could not locate a gnuradio python package dir." >&2
    echo "       pass --py-dest <dir>/gnuradio/kyttar explicitly." >&2
    exit 1
  fi
fi

# --- locate the GRC blocks dir(s) ---------------------------------------------
# GRC scans SEVERAL block dirs and a LATER one overwrites an earlier (the
# precedence trap). If we refresh only the highest-precedence dir, a stale copy in
# a lower one is a latent trap (and confuses anyone inspecting). So deploy to EVERY
# existing GRC block dir. The user may still pin one with --grc-dest.
GRC_DESTS=()
if [ -n "$GRC_DEST" ]; then
  GRC_DESTS=("$GRC_DEST")
else
  for d in /usr/local/share/gnuradio/grc/blocks \
           "$HOME/.local/share/gnuradio/grc/blocks" \
           /usr/share/gnuradio/grc/blocks; do
    [ -d "$d" ] && GRC_DESTS+=("$d")
  done
  [ ${#GRC_DESTS[@]} -eq 0 ] && GRC_DESTS=("/usr/local/share/gnuradio/grc/blocks")
fi
GRC_DEST="${GRC_DESTS[0]}"   # primary (for the summary line)

writable() { [ -w "$1" ] || { [ ! -e "$1" ] && [ -w "$(dirname "$1")" ]; }; }

PY_ROOT=1; writable "$(dirname "$PY_DEST")" && PY_ROOT=0
GRC_ROOT=1; writable "$GRC_DEST" && GRC_ROOT=0

TS="$(python3 -c 'import time;print(time.strftime("%Y%m%d-%H%M%S"))')"

say "Kyttar GRC install — repo is the source of truth"
say "  python module : $SRC_PY"
say "                  -> $PY_DEST   (root needed: $PY_ROOT)"
say "  GRC blocks    : $SRC_GRC/*.block.yml ($(ls "$SRC_GRC"/*.block.yml | wc -l) files)"
say "                  -> $GRC_DEST  (root needed: $GRC_ROOT)"
say ""

# --- deploy the python module (backup the old, copy the canonical tree) -------
say "[1/3] python module"
if [ -e "$PY_DEST" ]; then
  run "$PY_ROOT" cp -a "$PY_DEST" "$PY_DEST.bak-$TS"
  say "  backed up existing -> $PY_DEST.bak-$TS"
fi
# Copy only .py sources (no __pycache__); preserve the dir.
run "$PY_ROOT" mkdir -p "$PY_DEST"
if [ "$DRY_RUN" = 1 ]; then
  say "  [dry-run] would copy $(ls "$SRC_PY"/*.py | wc -l) .py files into $PY_DEST"
else
  TMP="$(mktemp -d)"
  cp "$SRC_PY"/*.py "$TMP/"
  run "$PY_ROOT" cp "$TMP"/*.py "$PY_DEST/"
  rm -rf "$TMP"
  # purge stale .pyc so the new sources are used
  run "$PY_ROOT" rm -rf "$PY_DEST/__pycache__"
  # Make world-readable: a sudo copy inherits root's umask and can land 0600
  # (root-only), which then breaks `import` for the running user. a+rX fixes it.
  run "$PY_ROOT" chmod -R a+rX "$PY_DEST"
fi
say "  done."

# --- deploy the GRC block definitions to EVERY GRC dir ------------------------
say "[2/3] GRC block definitions"
for gdest in "${GRC_DESTS[@]}"; do
  groot=1; writable "$gdest" && groot=0
  say "  -> $gdest (root needed: $groot)"
  run "$groot" mkdir -p "$gdest"
  if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] would copy $(ls "$SRC_GRC"/*.block.yml | wc -l) .block.yml into $gdest"
  else
    for y in "$SRC_GRC"/*.block.yml; do
      run "$groot" cp "$y" "$gdest/"
    done
    # same readability guard as the python module (sudo umask can land 0600)
    run "$groot" chmod a+rX "$gdest"/*.block.yml
  fi
done
say "  done."

# --- clear the GRC parse cache so new defs are loaded -------------------------
say "[3/3] clear GRC parse cache"
run 0 rm -rf "$HOME/.cache/gnuradio/grc"
say "  done."

say ""
if [ "$DRY_RUN" = 1 ]; then
  say "Dry run complete — nothing changed."
else
  say "Install complete. Verify in python:"
  say "  python3 -c 'from gnuradio import kyttar; print(kyttar.rx_batch)'"
  say "Then open a flowgraph in gnuradio-companion; the [Kyttar] blocks are current."
fi
