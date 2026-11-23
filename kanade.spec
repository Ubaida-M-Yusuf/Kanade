# PyInstaller spec for Kanade. Build with build.bat.
# One-folder build: dist\Kanade\Kanade.exe starts instantly (no unpacking on every launch)
# and leaves room for dist\Kanade\ffmpeg\ffmpeg.exe + ffprobe.exe next to it.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[("kanade/ui", "ui"), ("kanade/analyzer", "analyzer"), ("kanade/data", "data")],
    hiddenimports=collect_submodules("webview") + ["rapidfuzz.fuzz", "rapidfuzz.process"],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kanade",
    icon="kanade.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="Kanade")
