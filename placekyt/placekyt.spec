# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for placeKYT + simKYT (#163).

Builds TWO executables from one analysis-shared build:
  * ``placekyt``     — the GUI (main.py → QApplication + MainWindow)
  * ``placekyt-cli`` — the headless CLI (cli.py → build/--test/disasm)

Both bundle the simKYT runtime: the pure-Python ``gr_kyttar`` block library and
the compiled ``simkyt`` Rust extension (``simkyt.cpython-*.so``), plus the
chip-type YAML and icon resources. Resource access goes through ``resources.py``
(``sys._MEIPASS`` aware), so no ``Path(__file__).parent`` breaks under freeze.

Build (from the placekyt/ dir, with its venv active so PySide6 + the editable
gr_kyttar/simkyt installs are importable):

    .venv/bin/pyinstaller placekyt.spec --noconfirm

Output: ``dist/placekyt/`` (onedir — a folder with the launcher + libs; faster
start than onefile and easier to verify the native .so is present).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

SPEC_DIR = Path(SPECPATH)                       # placekyt/
SIMKYT_PY = (SPEC_DIR.parent / "simkyt" / "python").resolve()

block_cipher = None

# --- simKYT runtime: the gr_kyttar block library + the simkyt native ext ---
# gr_kyttar is pure Python but its blocks are discovered by iterating the
# already-imported kyttar_block module, so collect the package's submodules to
# be safe (placement/, bitstream/, the per-DSP modules). simkyt ships a
# compiled .so submodule that PyInstaller's static analysis cannot see — pull it
# in explicitly as a binary.
hiddenimports = (
    collect_submodules("gr_kyttar")
    + collect_submodules("simkyt")
    + ["simkyt.simkyt"]                   # the compiled submodule
)
binaries = collect_dynamic_libs("simkyt")    # the .cpython-*.so

# --- bundled data: chip types + icons (resolved via resources.resource_path) ---
datas = [
    (str(SPEC_DIR / "resources" / "chips"), "resources/chips"),
    (str(SPEC_DIR / "resources" / "icons"), "resources/icons"),
]

a = Analysis(
    ["main.py"],                                # GUI entry; CLI added as a 2nd EXE
    pathex=[str(SPEC_DIR), str(SIMKYT_PY)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)

# A second analysis for the CLI entry (shares the same collected libs/data).
a_cli = Analysis(
    ["cli.py"],
    pathex=[str(SPEC_DIR), str(SIMKYT_PY)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="placekyt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                              # GUI: no console window
    icon=str(SPEC_DIR / "resources" / "icons" / "lattrex_logo.png"),
)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="placekyt-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                              # CLI: keep the console
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="placekyt",
)
