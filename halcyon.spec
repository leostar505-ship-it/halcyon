import os
from PyInstaller.utils.hooks import collect_submodules

root = os.path.abspath(os.getcwd())
binaries = []
whisper = os.path.join(root, "vendor", "whisper", "whisper-server.exe")
if os.path.exists(whisper):
    binaries.append((whisper, "whisper"))

a = Analysis(
    [os.path.join(root, "run_halcyon.py")],
    pathex=[os.path.join(root, "src")],
    binaries=binaries,
    datas=[(os.path.join(root, "assets"), "assets"), (os.path.join(root, "tools"), "tools")],
    hiddenimports=collect_submodules("halcyon") + ["serial.tools.list_ports", "sounddevice", "mss"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Halcyon",
    icon=os.path.join(root, "assets", "icon.ico"),
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
