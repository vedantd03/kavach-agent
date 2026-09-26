# PyInstaller spec for the Kavach laptop agent.
#
#   pyinstaller kavach-agent.spec --noconfirm            one-file  (default)
#   KAVACH_ONEDIR=1 pyinstaller kavach-agent.spec --noconfirm    one-folder
#
# One-file is a single .exe that unpacks to a temp dir on each launch (slower
# start).  One-folder starts instantly and is easier to debug; both put
# .env and agent.db next to the executable (see agent/config.py::_base_dir).
#
# Rule 0 still holds: no OCR or ML libraries are bundled.  Streamlit, pandas,
# FastAPI and the test tooling are excluded on purpose - the 1A agent does not
# import them, and they triple the size.

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

ONEDIR = os.environ.get("KAVACH_ONEDIR", "").strip().lower() in {"1", "true", "yes"}

datas = [("agent/schema.sql", "agent")]      # store.py reads it next to the module
binaries = []

# detect-core is installed editable (`pip install -e ../kavach-server/detect_core`),
# and an editable install is invisible to PyInstaller's module graph: it resolves
# through a .pth finder rather than a real directory in site-packages.  Point the
# analysis at the source tree and name the modules explicitly.
DETECT_CORE_SRC = os.path.abspath(
    os.environ.get("DETECT_CORE_SRC", os.path.join("..", "kavach-server", "detect_core"))
)
hiddenimports = ["detect_core", "detect_core.contracts"]

# pypdfium2 ships the native PDF renderer used for page.to_image(); pdfminer
# carries the cmap tables that pdfplumber needs for text extraction.
for package in ("pypdfium2", "pdfplumber"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
datas += collect_data_files("pdfminer")

EXCLUDES = [
    "streamlit", "pandas", "numpy", "matplotlib", "fastapi", "uvicorn", "starlette",
    "pytest", "reportlab", "IPython", "tkinter", "PySide6", "PyQt5", "notebook",
    "google", "google_genai", "torch", "transformers", "pytesseract", "psutil",
]

analysis = Analysis(
    ["run_agent.py"],
    pathex=[DETECT_CORE_SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

if ONEDIR:
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="kavach-agent",
        console=True,
        strip=False,
        upx=False,
    )
    coll = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="kavach-agent",
    )
else:
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name="kavach-agent",
        console=True,
        strip=False,
        upx=False,
        onefile=True,
    )
