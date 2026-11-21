import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

root = os.path.abspath(os.getcwd())
binaries = []
cue_datas, cue_bins, cue_hidden = [], [], []
try:
    cue_datas, cue_bins, cue_hidden = collect_all("cuesdk")
except Exception:
    pass
binaries += cue_bins
whisper = os.path.join(root, "vendor", "whisper", "whisper-server.exe")
if os.path.exists(whisper):
    binaries.append((whisper, "whisper"))

a = Analysis(
    [os.path.join(root, "run_halcyon.py")],
    pathex=[os.path.join(root, "src")],
    binaries=binaries,
    datas=[(os.path.join(root, "assets"), "assets"), (os.path.join(root, "tools"), "tools")] + cue_datas,
    hiddenimports=collect_submodules("halcyon") + collect_submodules("numpy") + ["serial.tools.list_ports", "sounddevice", "mss", "openrgb", "openrgb.utils"] + cue_hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Halcyon",
    icon=os.path.join(root, "assets", "icon.ico"),
    console=False,
    upx=False,
)
dbg = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HalcyonDebug",
    icon=os.path.join(root, "assets", "icon.ico"),
    console=True,
    upx=False,
)
coll = COLLECT(exe, dbg, a.binaries, a.datas, name="Halcyon", upx=False)
