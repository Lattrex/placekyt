#!/usr/bin/env bash
# Build the placeKYT + simKYT standalone bundle with PyInstaller (#163).
#
# Produces dist/placekyt/ containing two executables:
#   placekyt      — the GUI
#   placekyt-cli  — the headless CLI (build / --test / disasm)
# both with the simKYT runtime baked in (gr_kyttar + the simkyt native .so)
# plus the chip-type YAML and icons.
#
# Run from the placekyt/ package root. Uses the package venv (.venv) so PySide6
# and the editable gr_kyttar/simkyt installs are importable.
#
#   scripts/build_bundle.sh
#
# After it finishes it smoke-tests the bundled CLI by building + comparing the
# QAM16 demo, so a green run means the native extension and block library are
# correctly bundled.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VENV_PY="$HERE/.venv/bin/python"
PYINSTALLER="$HERE/.venv/bin/pyinstaller"
CT="$HERE/resources/chips/kyttar_10x12.yaml"

if [ ! -x "$VENV_PY" ]; then
  echo "error: $VENV_PY not found — create the placekyt venv first." >&2
  exit 1
fi

# Ensure PyInstaller is present (idempotent).
if ! "$VENV_PY" -c "import PyInstaller" 2>/dev/null; then
  echo ">> installing pyinstaller into the venv"
  "$VENV_PY" -m pip install pyinstaller >/dev/null
fi

echo ">> building bundle (this takes a minute)"
"$PYINSTALLER" placekyt.spec --noconfirm --log-level WARN

echo ">> smoke-testing the bundled CLI (QAM16 demo build + golden compare)"
./dist/placekyt/placekyt-cli --test tests/data/demo/qam16_demo.kyt \
  --chip-type "$CT"

echo ""
echo ">> OK — bundle at dist/placekyt/"
echo "   GUI : ./dist/placekyt/placekyt"
echo "   CLI : ./dist/placekyt/placekyt-cli --help"
