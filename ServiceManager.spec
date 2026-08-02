# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)
datas = [(str(root / "icon.ico"), "."), *collect_data_files("tzdata")]
hiddenimports = ["paramiko", "croniter", "tzdata", "pystray._win32"]

analysis = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="PythonServiceManager",
    console=False,
    icon=str(root / "icon.ico"),
    upx=True,
)
